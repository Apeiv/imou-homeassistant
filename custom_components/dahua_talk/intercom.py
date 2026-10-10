"""Bộ đàm: mic điện thoại (thẻ WebRTC Camera qua go2rtc) → loa camera, ngay trong HA.

Thẻ ``custom:webrtc-camera`` có ``media: video,audio,microphone`` gửi mic vào
go2rtc; go2rtc (từ 1.9.10) đẩy tiếng vào stdin một lệnh ``exec:…#backchannel=1``;
lệnh ấy (ffmpeg) POST luồng A-law 8 kHz tới view dưới đây, và view phát ra loa
camera qua ``Speaker`` — dùng chung khoá phiên với TTS và thông báo.

Kênh nói chỉ mở KHI CÓ TIẾNG NGƯỜI và đóng sau một quãng im: camera tự tắt mic
của nó suốt lúc kênh nói mở, mà trình duyệt gửi tiếng liên tục suốt lúc thẻ còn
mở — mở kênh suốt thì không bao giờ nghe được người bên camera trả lời. Trừ đường HCNetSDK có
``nghe`` (đàm thoại HAI CHIỀU): tiếng mic camera về ngay trên kênh đàm thoại (``Nghe`` → ``NgheView``
cho thẻ), nên kênh mở suốt lượt bộ đàm, không VOX.

``CallView`` gộp cả hai chiều vào một WebSocket (``/api/dahua_talk/call_ws/<media_player>``, chỉ HCNetSDK):
mic thẻ lên loa, tiếng camera xuống thẻ, mỗi camera một cuộc, tối đa ``TOI_DA_NOI_GIAY`` giây.

go2rtc không gửi được header ``Authorization`` từ lệnh exec, nên đường POST dùng
khoá ngẫu nhiên riêng từng camera trong URL; khoá chỉ phát được tiếng ra loa của
đúng camera ấy.
"""

from __future__ import annotations

import array
import asyncio
import hmac
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .speaker import NGUONG_IM_DB, muc_db
from .talk import FFMPEG_TRUC_TIEP, TAN_SO

if TYPE_CHECKING:
    from .speaker import Speaker

_LOGGER = logging.getLogger(__name__)

#: Mức coi là có tiếng người (dBFS, RMS từng khúc). Mic tắt trong trình duyệt gửi
#: số 0; phòng yên sau lọc ồn của trình duyệt dưới -55.
NGUONG_DB = NGUONG_IM_DB
#: Im ngần này giây thì đóng kênh để nghe bên kia.
IM_GIAY = 1.5
#: Giữ ngần này giây tiếng ngay trước lúc có tiếng — khỏi mất âm đầu câu.
DEM_GIAY = 0.3
#: Loa hỏng (camera từ chối kênh, chương trình trợ giúp chết): chờ ngần này giây rồi mới mở lại (kênh hai chiều).
_CHO_LOI = 2.0
#: Một lần mở kênh nói dài tối đa ngần này giây; quá thì đóng gọn và phiên bộ đàm này thôi phát. Kênh hai chiều
#: không có VOX: quên tắt "nói" thì loa camera phát tiếng phòng mãi và camera tắt luôn tiếng RTSP của nó (bản ghi câm).
TOI_DA_NOI_GIAY = 180.0
#: Mỗi người nghe giữ tối đa ngần này khúc (khúc ≤ 4096 byte ≈ 0,26 s ở 8 kHz → ~4 s); chậm hơn thì bỏ khúc mới.
TOI_DA_KHUC_NGHE = 16


def _bang_alaw() -> list[int]:
    """G.711 A-law → PCM16 (đúng ``alaw2linear`` bản mẫu ITU; khớp ffmpeg 256/256 mã)."""
    bang = []
    for a in range(256):
        a ^= 0x55
        t = (a & 0x0F) << 4
        seg = (a & 0x70) >> 4
        t = t + 8 if seg == 0 else (t + 0x108) << (seg - 1)
        bang.append(t if a & 0x80 else -t)
    return bang


_ALAW = _bang_alaw()


def alaw_to_pcm(b: bytes) -> bytes:
    return array.array("h", (_ALAW[x] for x in b)).tobytes()


class Intercom:
    """Một phiên bộ đàm: nhận PCM16 ``tan_so`` Hz, mở loa khi có tiếng, đóng khi im."""

    def __init__(self, hass: HomeAssistant, speaker: "Speaker", tan_so: int = TAN_SO) -> None:
        self.hass, self.speaker, self.tan_so = hass, speaker, tan_so
        self.seconds = 0.0            # tổng số giây đã phát ra loa
        self._loi_luc = float("-inf")  # lúc loa hỏng gần nhất: chờ ``_CHO_LOI`` giây rồi mới mở lại
        self._hang: asyncio.Queue[bytes | None] | None = None
        self._mo_luc = 0.0            # lúc mở kênh nói hiện tại (``TOI_DA_NOI_GIAY``)
        self.het_gio = False          # đã quá ``TOI_DA_NOI_GIAY``: bỏ mọi tiếng tới cuối phiên
        self._viec: list[asyncio.Task] = []
        self._dem: list[tuple[float, bytes]] = []     # (lúc tới, PCM) ngay trước tiếng người
        self._im = 0.0

    async def _nguon(self, hang: asyncio.Queue) -> AsyncIterator[tuple[float, bytes]]:
        while (khuc := await hang.get()) is not None:
            yield khuc

    async def _phat(self, hang: asyncio.Queue) -> None:
        try:
            self.seconds += await self.speaker.async_play_pcm(self._nguon(hang), self.tan_so,
                                                              song=True)
        except Exception as exc:  # noqa: BLE001 — loa hỏng một lượt không được giết cả phiên
            _LOGGER.warning("intercom: cannot play to camera: %s", exc)
            self._loi_luc = time.monotonic()
            if self._hang is hang:    # kênh mở suốt (hai chiều): không ai đọc nữa → đóng, lượt sau mở lại
                self.stop_talking()
            # Nguồn còn đang đợi thì rút cạn để khỏi treo người đẩy.
            while not hang.empty():
                hang.get_nowait()

    def feed(self, pcm: bytes) -> None:
        pcm = pcm[: len(pcm) // 2 * 2]
        if not pcm or self.het_gio:
            return
        luc = time.monotonic()
        if self._hang is not None and luc - self._mo_luc >= TOI_DA_NOI_GIAY:
            _LOGGER.info("intercom: talk channel open for %.0f s, closing it for this session", TOI_DA_NOI_GIAY)
            self.het_gio = True
            self.stop_talking()
            return
        # Đàm thoại hai chiều (HCNetSDK, có thẻ đang nghe ``NgheView``): kênh mở từ khúc đầu và giữ suốt — tiếng
        # camera về qua kênh đàm thoại, không cần đóng để nghe. Không ai nghe thì VOX như cũ. Xét từng khúc.
        hai_chieu = self.speaker.hai_chieu and luc - self._loi_luc > _CHO_LOI
        co_tieng = hai_chieu or muc_db(pcm) > NGUONG_DB
        self._im = 0.0 if co_tieng else self._im + len(pcm) / (2 * self.tan_so)
        if self._hang is None:
            if not co_tieng:
                self._dem.append((luc, pcm))
                while sum(len(p) for _t, p in self._dem[1:]) >= DEM_GIAY * self.tan_so * 2:
                    self._dem.pop(0)
                return
            self._hang, self._mo_luc = asyncio.Queue(), luc
            for muc in self._dem:                   # đệm đầu câu giữ đúng lúc tới của nó
                self._hang.put_nowait(muc)
            self._dem = []
            self._hang.put_nowait((luc, pcm))
            # Tạo việc SAU khi đổ hàng: HA chạy task ngay (eager) — loa hỏng tức thì thì ``_hang`` đã về None.
            self._viec = [v for v in self._viec if not v.done()]     # thử lại mỗi ``_CHO_LOI`` giây: đừng tích mãi
            self._viec.append(self.hass.async_create_task(self._phat(self._hang)))
        else:
            self._hang.put_nowait((luc, pcm))
        if self._im >= IM_GIAY:
            self.stop_talking()

    @property
    def dang_noi(self) -> bool:
        """Kênh nói đang mở (loa camera bận)."""
        return self._hang is not None

    def stop_talking(self) -> None:
        """Đóng kênh nói (camera nghe lại được). Nói tiếp thì mở lại."""
        if self._hang is not None:
            self._hang.put_nowait(None)
            self._hang = None
        self._dem, self._im = [], 0.0

    async def async_close(self) -> None:
        self.stop_talking()
        if self._viec:
            await asyncio.gather(*self._viec)


class IntercomView(HomeAssistantView):
    """go2rtc POST luồng A-law 8 kHz vào đây (khoá riêng từng camera trong URL)."""

    url = "/api/dahua_talk/intercom/{entry_id}"
    name = "api:dahua_talk:intercom"
    requires_auth = False

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(entry_id)
        khoa = str(entry.data.get(CONF_INTERCOM_KEY) or "") if entry and entry.domain == DOMAIN else ""
        gui = request.query.get("k", "")
        if not khoa or not hmac.compare_digest(khoa.encode(), gui.encode()) \
                or getattr(entry, "runtime_data", None) is None:
            return web.Response(status=401)
        phien = Intercom(hass, entry.runtime_data.speaker)
        _LOGGER.debug("intercom %s: open", entry.title)
        try:
            while khuc := await request.content.readany():
                phien.feed(alaw_to_pcm(khuc))
        finally:
            await phien.async_close()
            _LOGGER.debug("intercom %s: closed, %.1f s played", entry.title, phien.seconds)
        return web.json_response({"seconds": round(phien.seconds, 1)})


CONF_INTERCOM_KEY = "intercom_key"


def muc_theo_entity(hass: HomeAssistant, entity_id: str) -> ConfigEntry | None:
    """Mục cấu hình của tích hợp mà ``entity_id`` thuộc về; không phải của tích hợp này thì None."""
    rec = er.async_get(hass).async_get(entity_id)
    entry = hass.config_entries.async_get_entry(rec.config_entry_id) if rec else None
    return entry if entry is not None and entry.domain == DOMAIN else None


class Nghe:
    """Tiếng MIC CAMERA về trong lúc kênh đàm thoại mở (HCNetSDK ``nghe``): phát cho mọi trình duyệt đang
    nghe qua ``NgheView``. ``feed`` gọi từ luồng bất kỳ; mỗi người nghe một hàng đợi."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.nguoi_nghe: set[asyncio.Queue[bytes | None]] = set()   # None = hết (gỡ mục)
        self.da_dong = False            # mục đã gỡ: không nhận người nghe mới

    @property
    def co_nguoi_nghe(self) -> bool:
        return bool(self.nguoi_nghe)

    def feed(self, pcm: bytes) -> None:
        self.hass.loop.call_soon_threadsafe(self._phat, pcm)

    def _phat(self, pcm: bytes) -> None:
        for hang in self.nguoi_nghe:
            if hang.qsize() < TOI_DA_KHUC_NGHE:      # người nghe chết lặng: bỏ khúc, không tích vô hạn
                hang.put_nowait(pcm)

    def close(self) -> None:
        """Gỡ mục: thả mọi người đang nghe — kể cả người có hàng đợi đầy (``None`` không bị bỏ)."""
        self.da_dong = True
        for hang in self.nguoi_nghe:
            hang.put_nowait(None)


def _nghe_theo_entity(hass: HomeAssistant, entity_id: str) -> tuple[ConfigEntry | None, Nghe | None]:
    """Mục và ``Nghe`` của ``entity_id`` — ``Nghe`` là None khi không phải HCNetSDK hay mục đã gỡ."""
    entry = muc_theo_entity(hass, entity_id)
    nghe = getattr(getattr(entry, "runtime_data", None), "mic_camera", None)
    return entry, (nghe if nghe is not None and not nghe.da_dong else None)


# ponytail: NgheView và IntercomView còn giữ tới khi thẻ mới (``CallView``) được thử thật; sau đó bỏ được
# nếu không ai dùng.
class NgheView(HomeAssistantView):
    """Thẻ GET (token HA) → luồng PCM16 LE mono ``TAN_SO`` Hz tiếng camera trong lúc nói, tới khi thẻ ngắt."""

    url = "/api/dahua_talk/listen/{entity_id}"
    name = "api:dahua_talk:listen"

    async def get(self, request: web.Request, entity_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        _entry, nghe = _nghe_theo_entity(hass, entity_id)
        if nghe is None:
            return web.Response(status=404)
        resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "Cache-Control": "no-store"})
        hang: asyncio.Queue[bytes | None] = asyncio.Queue()
        nghe.nguoi_nghe.add(hang)                    # trước ``await``: gỡ mục lúc ấy vẫn thả được hàng này
        dau = True
        # Thẻ ngắt (thôi nói): HA huỷ handler (handler_cancellation) hay ``write`` báo lỗi — đều qua ``finally``.
        try:
            await resp.prepare(request)
            while (pcm := await hang.get()) is not None:
                if dau:
                    dau = False
                    _LOGGER.debug("listen: audio from the camera is flowing")
                await resp.write(pcm)
        except ConnectionResetError:
            pass
        finally:
            nghe.nguoi_nghe.discard(hang)
        return resp


#: Mục đang có cuộc gọi qua ``CallView`` — mỗi mục một cuộc.
_DANG_GOI: set[str] = set()
#: Byte đầu mỗi khung nhị phân của ``CallView``: tiếng camera xuống thẻ / mic thẻ lên loa camera.
KHUNG_XUONG = b"\x01"
KHUNG_LEN = b"\x02"
#: Mã đóng WebSocket: đang bận (cuộc khác hay loa đang phát) / hết ``TOI_DA_NOI_GIAY``.
MA_BAN, MA_HET_GIO = 4409, 4408


class CallView(HomeAssistantView):
    """Thẻ mở WebSocket (token HA hoặc đường ký ``auth/sign_path``; mọi người dùng đã đăng nhập, không chỉ admin)
    → đàm thoại hai chiều trên một kết nối. Chỉ HCNetSDK (``mic_camera``), khác thì 404 trước khi nâng cấp.

    Sau khi nâng cấp HA gửi chữ ``{"type": "ready"}``. Thẻ → HA: ``KHUNG_LEN`` + PCM16 LE mono ``TAN_SO`` Hz →
    ``Intercom`` (kênh nói chỉ mở ở khung đầu tiên); khung khác thì bỏ. HA → thẻ: tiếng mic camera (``Nghe``)
    thành ``KHUNG_XUONG`` + PCM. Mỗi mục một cuộc, loa đang phát cũng tính là bận: đóng ngay mã ``MA_BAN``.
    Quá ``TOI_DA_NOI_GIAY`` giây: đóng mã ``MA_HET_GIO``."""

    url = "/api/dahua_talk/call_ws/{entity_id}"
    name = "api:dahua_talk:call_ws"

    async def get(self, request: web.Request, entity_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        entry, nghe = _nghe_theo_entity(hass, entity_id)
        if nghe is None:
            return web.Response(status=404)
        # Ping 30 s: Cloudflare đóng WebSocket lặng ~100 s. Khung 64 KiB là thừa cho mic (~4 s PCM 8 kHz).
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 16)
        loa = entry.runtime_data.speaker
        if entry.entry_id in _DANG_GOI or loa.playing:     # TTS, thông báo hay bộ đàm go2rtc đang giữ loa
            await ws.prepare(request)
            await ws.close(code=MA_BAN)
            return ws
        _DANG_GOI.add(entry.entry_id)
        try:
            await self._goi(hass, request, ws, entry, nghe)
        finally:
            _DANG_GOI.discard(entry.entry_id)        # lỗi gì cũng không để mục "bận" mãi
        return ws

    async def _goi(self, hass: HomeAssistant, request: web.Request, ws: web.WebSocketResponse,
                   entry: ConfigEntry, nghe: Nghe) -> None:
        hang: asyncio.Queue[bytes | None] = asyncio.Queue()
        nghe.nguoi_nghe.add(hang)                    # trước ``await``: gỡ mục lúc ấy vẫn thả được hàng này
        phien = Intercom(hass, entry.runtime_data.speaker)

        async def _gui() -> None:
            while (pcm := await hang.get()) is not None:
                await ws.send_bytes(KHUNG_XUONG + pcm)

        async def _nhan() -> None:
            async for msg in ws:                     # khung khác ``KHUNG_LEN`` (kể cả chữ) thì bỏ qua
                if msg.type == WSMsgType.BINARY and msg.data[:1] == KHUNG_LEN:
                    phien.feed(msg.data[1:])

        viec: list[asyncio.Task] = []
        _LOGGER.debug("call %s: open", entry.title)
        try:
            await ws.prepare(request)
            await ws.send_json({"type": "ready"})
            viec = [asyncio.create_task(_gui()), asyncio.create_task(_nhan())]
            xong, _ = await asyncio.wait(viec, timeout=TOI_DA_NOI_GIAY, return_when=asyncio.FIRST_COMPLETED)
            if not xong:
                _LOGGER.info("call %s: %.0f s limit reached, hanging up", entry.title, TOI_DA_NOI_GIAY)
                await ws.close(code=MA_HET_GIO)
        finally:
            for v in viec:
                v.cancel()
            nghe.nguoi_nghe.discard(hang)
            for kq in await asyncio.gather(*viec, return_exceptions=True):
                if isinstance(kq, Exception):        # thẻ rớt mạng giữa chừng: bình thường
                    _LOGGER.debug("call %s: %r", entry.title, kq)
            await phien.async_close()
            if ws.prepared:
                await ws.close()
            _LOGGER.debug("call %s: closed, %.1f s played", entry.title, phien.seconds)


def go2rtc_source(hass: HomeAssistant, entry_id: str, key: str, ha_url: str = "") -> str:
    """Dòng nguồn dán vào go2rtc.yaml (cuối danh sách nguồn của luồng camera).

    ``ha_url`` = địa chỉ HA mà MÁY CHẠY go2rtc gọi tới được. Bỏ trống thì dùng
    ``127.0.0.1`` — chỉ đúng khi go2rtc dùng chung mạng với HA (add-on go2rtc của HA
    OS, hoặc cả hai container ``network_mode: host`` trên cùng máy). go2rtc ở máy/VM
    khác, trong Frigate, hay container mạng bridge thì phải truyền địa chỉ LAN của HA.

    go2rtc tách lệnh exec theo dấu cách và tham số theo '#': URL không được chứa hai
    ký tự ấy (khoá là token_urlsafe — không có).
    """
    goc = (ha_url or "").strip().rstrip("/")
    if not goc:
        http = getattr(hass, "http", None)
        cong = getattr(http, "server_port", None) or 8123
        # HA bật SSL trên cổng của nó thì phải gọi https (ffmpeg mặc định không kiểm
        # chứng chỉ, nên chứng chỉ cấp cho tên miền vẫn dùng được với 127.0.0.1).
        kieu = "https" if getattr(http, "ssl_certificate", None) else "http"
        goc = f"{kieu}://127.0.0.1:{cong}"
    url = f"{goc}/api/dahua_talk/intercom/{entry_id}?k={key}"
    return (f"exec:ffmpeg {' '.join(FFMPEG_TRUC_TIEP)} "
            f"-f alaw -ar 8000 -ac 1 -i - -c:a copy -f alaw -flush_packets 1 "    # G.711: 8 kHz theo định nghĩa
            f"-method POST {url}#backchannel=1#audio=alaw/8000")
