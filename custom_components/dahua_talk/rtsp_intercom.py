"""Bộ đàm 16 kHz: máy chủ RTSP nhỏ ngay trong HA nhận tiếng Opus của trình duyệt từ go2rtc.

Vì sao: đường ``exec:`` của go2rtc (``intercom.py``) chỉ đi được G.711 8 kHz — WebRTC của go2rtc
1.9.14 chỉ nhận từ trình duyệt Opus 48 kHz / PCMU / PCMA 8 kHz, còn ``exec`` ghi gói trần vào
stdin không giữ ranh giới gói nên Opus không giải mã được. Nguồn RTSP có rãnh kênh ngược (kiểu
ONVIF, ``a=sendonly``) khai ``opus/48000/2`` thì go2rtc chọn Opus với trình duyệt và chuyển
nguyên từng gói RTP sang (đo 29/09/2026: 379 gói PT 96 qua kênh xen TCP). Tích hợp giải mã bằng
PyAV (HA có sẵn cho ``stream``) ra PCM đúng tần số loa (16 kHz với Imou 8086 / HCNetSDK) —
tiếng không còn bị cắt ở 4 kHz.

Dòng dán vào go2rtc.yaml: ``rtsp://<HA>:8557/<entry_id>/<khoá>#backchannel=1`` (khoá bộ đàm
riêng từng camera, như đường HTTP). Chỉ nhận RTP xen trong kết nối TCP (go2rtc mặc định vậy).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .intercom import CONF_INTERCOM_KEY, Intercom

_LOGGER = logging.getLogger(__name__)

CONG_RTSP = 8557
_PT_OPUS = 96
#: Một khối yêu cầu RTSP (dòng đầu + header) dài hơn ngần này là không phải go2rtc — ngắt.
_TOI_DA_YEU_CAU = 8192
#: Sau PLAY trình duyệt gửi Opus liên tục (~50 gói/s); im ngần này giây là go2rtc giữ kết nối chết — đóng.
_CHO_RTP_GIAY = 15.0
#: Trước PLAY: ngần này giây không có lệnh nào (khớp ``Session: …;timeout=60``) thì đóng.
_CHO_LENH_GIAY = 60.0
_SDP = ("v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=dahua_talk\r\nt=0 0\r\n"
        f"m=audio 0 RTP/AVP {_PT_OPUS}\r\na=rtpmap:{_PT_OPUS} opus/48000/2\r\n"
        "a=control:trackID=0\r\na=sendonly\r\n")


def co_giai_ma_opus() -> bool:
    """HA có PyAV kèm bộ giải mã Opus (bản HA chính thức luôn có — ``stream`` dùng)."""
    try:
        import av
        av.CodecContext.create("opus", "r")
    except Exception:  # noqa: BLE001 — thiếu thư viện hay thiếu codec đều là "không dùng được"
        return False
    return True


class GiaiMaOpus:
    """Gói Opus (RTP payload) → PCM16 LE mono ``tan_so`` Hz."""

    def __init__(self, tan_so: int) -> None:
        import av
        self._av = av
        self._ctx = av.CodecContext.create("opus", "r")
        self._doi = av.AudioResampler(format="s16", layout="mono", rate=tan_so)

    def __call__(self, goi: bytes) -> bytes:
        ra = []
        try:
            for khung in self._ctx.decode(self._av.Packet(goi)):
                for k in self._doi.resample(khung):
                    ra.append(bytes(k.planes[0])[: k.samples * 2])
        except self._av.FFmpegError:
            return b""                               # gói hỏng: bỏ, không giết cả phiên
        return b"".join(ra)


def tai_trong_rtp(goi: bytes) -> tuple[int, bytes] | None:
    """(PT, dữ liệu) của một gói RTP; không phải RTP v2 hợp lệ thì None."""
    if len(goi) < 12 or goi[0] >> 6 != 2:
        return None
    dau = 12 + 4 * (goi[0] & 0x0F)
    if goi[0] & 0x10:                                # phần mở rộng
        if len(goi) < dau + 4:
            return None
        dau += 4 + 4 * int.from_bytes(goi[dau + 2:dau + 4], "big")
    cuoi = len(goi) - (goi[-1] if goi[0] & 0x20 else 0)
    return (goi[1] & 0x7F, goi[dau:cuoi]) if dau <= cuoi else None


class MayChuBoDam:
    """Máy chủ RTSP cổng ``CONG_RTSP`` cho mọi camera của tích hợp (một cổng, phân theo đường)."""

    def __init__(self, hass: HomeAssistant, port: int = CONG_RTSP) -> None:
        self.hass, self.port = hass, port
        self._sv: asyncio.Server | None = None

    @property
    def dang_chay(self) -> bool:
        return self._sv is not None

    async def async_start(self) -> bool:
        if not co_giai_ma_opus():
            _LOGGER.warning("intercom RTSP: this Home Assistant has no Opus decoder (PyAV); "
                            "the go2rtc exec line (8 kHz) stays the intercom path")
            return False
        try:
            self._sv = await asyncio.start_server(self._ket_noi, port=self.port)
        except OSError as exc:
            _LOGGER.warning("intercom RTSP: cannot listen on port %s (%s); the go2rtc exec "
                            "line (8 kHz) stays the intercom path", self.port, exc)
            return False
        return True

    async def async_stop(self) -> None:
        sv, self._sv = self._sv, None
        if sv is not None:
            sv.close()
            await sv.wait_closed()

    def _loa(self, url: str):
        """``rtsp://host:cổng/<entry_id>/<khoá>[/trackID=0]`` → Speaker của camera, sai khoá thì None."""
        phan = [x for x in urlsplit(url).path.split("/") if x]
        if len(phan) < 2:
            return None
        entry = self.hass.config_entries.async_get_entry(phan[0])
        if entry is None or entry.domain != DOMAIN:
            return None
        khoa = str(entry.data.get(CONF_INTERCOM_KEY) or "")
        if not khoa or not hmac.compare_digest(khoa.encode(), phan[1].encode()):
            return None
        data = getattr(entry, "runtime_data", None)
        return (entry.title, data.speaker) if data is not None else None

    async def _ket_noi(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        loa = None                                   # (tên, Speaker) sau DESCRIBE đúng khoá
        kenh = 0                                     # kênh xen của RTP (SETUP interleaved=a-b)
        phien: Intercom | None = None
        giai_ma: GiaiMaOpus | None = None
        try:
            while True:
                # go2rtc giữ TCP mà không gửi gì (cả RTP lẫn GET_PARAMETER giữ phiên): đóng để nhả loa camera.
                async with asyncio.timeout(_CHO_RTP_GIAY if phien else _CHO_LENH_GIAY):
                    dau = await r.readexactly(1)
                if dau == b"$":                      # gói RTP xen trong TCP
                    ch_n = await r.readexactly(3)
                    goi = await r.readexactly(int.from_bytes(ch_n[1:3], "big"))
                    rtp = tai_trong_rtp(goi) if ch_n[0] == kenh and phien else None
                    if rtp and rtp[0] == _PT_OPUS:
                        phien.feed(giai_ma(rtp[1]))
                    continue
                khoi = dau + await r.readuntil(b"\r\n\r\n")
                if len(khoi) > _TOI_DA_YEU_CAU:
                    return
                dong = khoi.decode("utf-8", "replace").split("\r\n")
                lenh, url = (dong[0].split() + ["", ""])[:2]
                hdr = {k.strip().lower(): v.strip()
                       for k, _s, v in (x.partition(":") for x in dong[1:] if ":" in x)}
                if int(hdr.get("content-length", "0") or 0):
                    await r.readexactly(int(hdr["content-length"]))
                cseq = hdr.get("cseq", "0")
                them, than, ma = "", "", "200 OK"
                if lenh == "OPTIONS":
                    them = "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER\r\n"
                elif lenh == "DESCRIBE":
                    loa = self._loa(url)
                    if loa is None:
                        ma = "404 Not Found"
                    else:
                        than = _SDP
                        them = f"Content-Type: application/sdp\r\nContent-Base: {url}/\r\n"
                elif loa is None:
                    ma = "454 Session Not Found"
                elif lenh == "SETUP":
                    tr = hdr.get("transport", "")
                    xen = next((x.split("=", 1)[1] for x in tr.split(";")
                                if x.startswith("interleaved=")), None)
                    if "TCP" not in tr.upper() or xen is None:
                        ma = "461 Unsupported Transport"
                    else:
                        kenh = int(xen.split("-")[0])
                        them = f"Transport: {tr}\r\nSession: 1;timeout=60\r\n"
                elif lenh == "PLAY":
                    ten, speaker = loa
                    if phien is None:
                        phien = Intercom(self.hass, speaker, speaker.tan_so)
                        giai_ma = GiaiMaOpus(speaker.tan_so)
                        _LOGGER.debug("intercom %s: RTSP open (Opus → %s Hz)", ten, speaker.tan_so)
                    them = "Session: 1\r\n"
                elif lenh == "TEARDOWN":
                    w.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n\r\n".encode())
                    return
                w.write((f"RTSP/1.0 {ma}\r\nCSeq: {cseq}\r\n{them}"
                         f"Content-Length: {len(than.encode())}\r\n\r\n{than}").encode())
                await w.drain()
                if ma.startswith(("404", "454")):
                    return
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError, TimeoutError):
            pass
        finally:
            if phien is not None:
                await phien.async_close()
                _LOGGER.debug("intercom %s: RTSP closed, %.1f s played", loa[0], phien.seconds)
            w.close()


def go2rtc_rtsp_source(entry_id: str, key: str, host: str = "127.0.0.1",
                       port: int = CONG_RTSP) -> str:
    """Dòng nguồn go2rtc cho bộ đàm 16 kHz. ``#backchannel=1`` ghi rõ: go2rtc tắt kênh ngược của
    nguồn RTSP hễ URL có tuỳ chọn ``#`` nào khác (internal/rtsp/rtsp.go)."""
    return f"rtsp://{host}:{port}/{entry_id}/{key}#backchannel=1"
