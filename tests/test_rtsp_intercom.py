"""Bộ đàm 16 kHz: go2rtc đẩy Opus của trình duyệt qua kênh ngược RTSP → HA giải mã → loa.

Đo thật 29/09/2026 (go2rtc 1.9.14): nguồn RTSP khai rãnh ``a=sendonly`` ``opus/48000/2`` thì
go2rtc chọn Opus với trình duyệt và chuyển nguyên từng gói RTP (379 gói PT 96) qua kênh xen TCP.
"""

import asyncio
import math
import socket
import struct
from unittest import mock

import pytest

from homeassistant.helpers import entity_registry as er

from custom_components.dahua_talk import intercom, rtsp_intercom
from custom_components.dahua_talk.const import DOMAIN
from custom_components.dahua_talk.speaker import Speaker

from .test_intercom import LoaGia, _muc, _nap


def _goi_opus(hz: float, giay: float, db: float = -20.0) -> list[bytes]:
    """Tiếng ``hz`` mã hoá Opus 48 kHz như trình duyệt gửi (khung 20 ms). Cần PyAV (HA có sẵn
    cho ``stream``; môi trường test thiếu thì bỏ qua test ấy)."""
    av = pytest.importorskip("av")
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate, enc.layout, enc.format, enc.bit_rate = 48000, "mono", "s16", 64000
    bien = 32767 * 10 ** (db / 20) * math.sqrt(2)
    ra, n = [], 0
    for _ in range(int(giay * 50)):
        f = av.AudioFrame(format="s16", layout="mono", samples=960)
        f.planes[0].update(struct.pack("<960h", *(
            int(bien * math.sin(2 * math.pi * hz * (n + i) / 48000)) for i in range(960))))
        f.sample_rate, f.pts = 48000, n
        n += 960
        ra += [bytes(p) for p in enc.encode(f)]
    ra += [bytes(p) for p in enc.encode(None)]
    return ra


def _rtp(tai: bytes, seq: int) -> bytes:
    return struct.pack(">BBHII", 0x80, 96, seq & 0xFFFF, seq * 960, 0x1234) + tai


def _cong_suat(pcm: bytes, hz: float, tan_so: int) -> float:
    """Tỉ lệ năng lượng ở ``hz`` trên tổng (Goertzel) — gần 1 là tiếng đơn âm ``hz``."""
    x = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    k = 2 * math.cos(2 * math.pi * hz / tan_so)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + k * s1 - s2, s1
    tong = sum(v * v for v in x) or 1
    return (s1 * s1 + s2 * s2 - k * s1 * s2) / (tong * len(x) / 2)


def test_giai_ma_opus_giu_dai_tren_4_khz():
    """Tiếng 6 kHz — đường A-law 8 kHz cũ cắt mất hẳn — còn nguyên sau khi giải ra 16 kHz."""
    pytest.importorskip("av")
    gm = rtsp_intercom.GiaiMaOpus(16000)
    pcm = b"".join(gm(g) for g in _goi_opus(6000, 1.0))
    assert len(pcm) >= 2 * 16000 * 0.9
    assert _cong_suat(pcm[3200:], 6000, 16000) > 0.8


def test_goi_hong_khong_giet_phien():
    pytest.importorskip("av")
    assert rtsp_intercom.GiaiMaOpus(16000)(b"\xff\xff\xff") == b""


def test_tai_trong_rtp_bo_csrc_mo_rong_va_dem():
    tai = b"opus"
    goc = struct.pack(">BBHII", 0x80, 96, 1, 2, 3)
    assert rtsp_intercom.tai_trong_rtp(goc + tai) == (96, tai)
    csrc = struct.pack(">BBHII", 0x82, 96, 1, 2, 3) + b"\0" * 8
    assert rtsp_intercom.tai_trong_rtp(csrc + tai) == (96, tai)
    mo_rong = struct.pack(">BBHII", 0x90, 96, 1, 2, 3) + b"\xbe\xde\x00\x01" + b"\0" * 4
    assert rtsp_intercom.tai_trong_rtp(mo_rong + tai) == (96, tai)
    dem = struct.pack(">BBHII", 0xA0, 96, 1, 2, 3) + tai + b"\0\0\x03"
    assert rtsp_intercom.tai_trong_rtp(dem) == (96, tai)
    assert rtsp_intercom.tai_trong_rtp(b"\x00" * 20) is None


async def _hoi(r, w, lenh: str, url: str, them: str = "", cseq: int = 1) -> tuple[str, str]:
    w.write(f"{lenh} {url} RTSP/1.0\r\nCSeq: {cseq}\r\n{them}\r\n".encode())
    await w.drain()
    dau = (await r.readuntil(b"\r\n\r\n")).decode()
    n = int(next((x.split(":")[1] for x in dau.split("\r\n") if x.lower().startswith("content-length")), 0))
    return dau, (await r.readexactly(n)).decode() if n else ""


@pytest.fixture
async def may_chu(hass, socket_enabled):
    muc = _muc()
    with mock.patch("custom_components.dahua_talk.MayChuBoDam.async_start", return_value=False):
        await _nap(hass, muc)
    may = rtsp_intercom.MayChuBoDam(hass, port=0)
    with mock.patch.object(rtsp_intercom, "co_giai_ma_opus", return_value=True):
        assert await may.async_start()
    # cổng 0: IPv4 và IPv6 mỗi bên một số cổng khác nhau — lấy đúng cổng IPv4
    cong = next(x for x in may._sv.sockets if x.family == socket.AF_INET).getsockname()[1]
    yield muc, f"rtsp://127.0.0.1:{cong}/{muc.entry_id}", cong
    await may.async_stop()


@pytest.mark.may_chu_rtsp
async def test_go2rtc_day_opus_qua_rtsp_loa_nhan_16_khz(hass, may_chu):
    pytest.importorskip("av")
    muc, goc, cong = may_chu
    url = f"{goc}/{muc.data[intercom.CONF_INTERCOM_KEY]}"
    loa = LoaGia()
    with mock.patch.object(Speaker, "async_play_pcm", loa.async_play_pcm):
        r, w = await asyncio.open_connection("127.0.0.1", cong)
        assert "200 OK" in (await _hoi(r, w, "OPTIONS", url))[0]
        dau, sdp = await _hoi(r, w, "DESCRIBE", url, "Require: www.onvif.org/ver20/backchannel\r\n", 2)
        assert "200 OK" in dau and "opus/48000/2" in sdp and "a=sendonly" in sdp
        dau, _ = await _hoi(r, w, "SETUP", url + "/trackID=0",
                            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n", 3)
        assert "200 OK" in dau and "interleaved=0-1" in dau
        assert "200 OK" in (await _hoi(r, w, "PLAY", url, "Session: 1\r\n", 4))[0]
        goi = _goi_opus(6000, 1.0) + _goi_opus(440, intercom.IM_GIAY + 0.5, db=-90)   # nói rồi im
        for i, g in enumerate(goi):
            p = _rtp(g, i)
            w.write(b"$\x00" + struct.pack(">H", len(p)) + p)
        await w.drain()
        assert "200 OK" in (await _hoi(r, w, "TEARDOWN", url, "Session: 1\r\n", 5))[0]
        assert await r.read() == b""                              # máy chủ đóng sau khi phát xong
        w.close()
    assert len(loa.phien) == 1 and loa.tan_so == 16000             # loa Imou 8086: 16 kHz
    assert len(loa.phien[0]) >= 2 * 16000 * 0.9
    assert _cong_suat(loa.phien[0][6400:2 * 16000], 6000, 16000) > 0.8


@pytest.mark.may_chu_rtsp
async def test_sai_khoa_hay_udp_bi_tu_choi(hass, may_chu):
    muc, goc, cong = may_chu
    for url in (f"{goc}/sai-khoa", f"{goc}", "rtsp://127.0.0.1/khong-co/x"):
        r, w = await asyncio.open_connection("127.0.0.1", cong)
        assert "404" in (await _hoi(r, w, "DESCRIBE", url))[0]
        assert await r.read() == b""
        w.close()
    r, w = await asyncio.open_connection("127.0.0.1", cong)
    assert "454" in (await _hoi(r, w, "SETUP", goc + "/x/trackID=0",
                                "Transport: RTP/AVP/TCP;interleaved=0-1\r\n"))[0]
    w.close()
    url = f"{goc}/{muc.data[intercom.CONF_INTERCOM_KEY]}"
    r, w = await asyncio.open_connection("127.0.0.1", cong)
    await _hoi(r, w, "DESCRIBE", url)
    assert "461" in (await _hoi(r, w, "SETUP", url + "/trackID=0",
                                "Transport: RTP/AVP;unicast;client_port=5000-5001\r\n", 2))[0]
    w.close()


async def test_dich_vu_tra_nguon_rtsp_khi_may_chu_chay(hass):
    muc = _muc()
    with mock.patch("custom_components.dahua_talk.MayChuBoDam.async_start", return_value=True), \
            mock.patch("custom_components.dahua_talk.MayChuBoDam.dang_chay",
                       new_callable=mock.PropertyMock, return_value=True):
        await _nap(hass, muc)
        mp = next(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), muc.entry_id)
                  if e.domain == "media_player")
        khoa = muc.data[intercom.CONF_INTERCOM_KEY]
        kq = await hass.services.async_call(DOMAIN, "get_intercom_source", {"entity_id": mp},
                                            blocking=True, return_response=True)
        assert kq["source"] == f"rtsp://127.0.0.1:8557/{muc.entry_id}/{khoa}#backchannel=1"
        assert kq["exec_source"].startswith("exec:ffmpeg ")
        kq = await hass.services.async_call(DOMAIN, "get_intercom_source",
                                            {"entity_id": mp, "ha_url": "http://192.168.1.10:8123"},
                                            blocking=True, return_response=True)
        assert kq["source"] == f"rtsp://192.168.1.10:8557/{muc.entry_id}/{khoa}#backchannel=1"


@pytest.mark.may_chu_rtsp
async def test_ranh_sau_play_giu_ket_noi_dang_noi_ma_im_thi_dong(hass, may_chu):
    """go2rtc giữ kênh ngược rảnh suốt: không được đóng (đóng là nó nối lại, vòng EOF mỗi 15 s).
    Kênh nói đang mở mà hết RTP thì vẫn đóng để nhả loa camera."""
    muc, goc, cong = may_chu
    url = f"{goc}/{muc.data[intercom.CONF_INTERCOM_KEY]}"
    tieng = struct.pack("<320h", *(int(3000 * math.sin(2 * math.pi * i / 16)) for i in range(320)))
    loa = LoaGia()

    async def toi_play():
        r, w = await asyncio.open_connection("127.0.0.1", cong)
        await _hoi(r, w, "DESCRIBE", url)
        await _hoi(r, w, "SETUP", url + "/trackID=0", "Transport: RTP/AVP/TCP;interleaved=0-1\r\n", 2)
        assert "200 OK" in (await _hoi(r, w, "PLAY", url, "Session: 1\r\n", 3))[0]
        return r, w

    async def noi_roi_im(r, w):
        for i in range(5):
            p = _rtp(tieng, i)
            w.write(b"$\x00" + struct.pack(">H", len(p)) + p)
        await w.drain()
        assert await asyncio.wait_for(r.read(), 2) == b""         # máy chủ đóng sau ~0,2 s
        w.close()

    with mock.patch.object(Speaker, "async_play_pcm", loa.async_play_pcm), \
            mock.patch.object(rtsp_intercom, "_CHO_RTP_GIAY", 0.2), \
            mock.patch.object(rtsp_intercom, "GiaiMaOpus", lambda _tan_so: lambda tai: tai):
        r, w = await toi_play()
        await asyncio.sleep(0.5)                                  # rảnh quá hạn: vẫn mở
        assert "200 OK" in (await _hoi(r, w, "OPTIONS", url, cseq=4))[0]
        await noi_roi_im(r, w)                                    # có tiếng → mở loa, rồi im hẳn
        assert len(loa.phien) == 1 and len(loa.phien[0]) == 5 * len(tieng)
        # Quá ``TOI_DA_NOI_GIAY``: phiên câm tới cuối — im thì đóng, go2rtc nối lại được phiên mới.
        with mock.patch.object(intercom, "TOI_DA_NOI_GIAY", 0):
            await noi_roi_im(*await toi_play())
