"""Loa của một camera, phía Home Assistant: nhận âm thanh (luồng PCM, WAV, URL) rồi phát.

Cách nói với camera (Imou 8086 16 kHz, Dahua 37777 hay kênh ngược RTSP) do ``mo_phien`` quyết;
mỗi phiên khai tần số của nó (``tan_so``). Loa sinh tiếng đúng tần số ấy; nguồn có tần số
khác (bộ đàm A-law 8 kHz) thì đổi ngay trước khi gửi.

Mỗi camera một lúc chỉ một phiên nói (``asyncio.Lock``): thông báo và câu trả lời
Assist cùng tới thì lần lượt. Phiên nói chạy trong luồng executor vì giao thức
chặn (socket đồng bộ, phát đúng nhịp thời gian thực).
"""

from __future__ import annotations

import asyncio
import io
import logging
import queue
import time
import wave
from array import array
from collections.abc import AsyncIterator, Callable

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant

from .talk import KHOI, TAN_SO

_LOGGER = logging.getLogger(__name__)


def doi_tan_so(pcm: bytes, vao: int, ra: int) -> bytes:
    """PCM16 mono ``vao`` Hz → ``ra`` Hz, nội suy tuyến tính (đủ cho tiếng nói 8↔16 kHz)."""
    if vao == ra or not pcm:
        return pcm
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    n = len(a)
    m = n * ra // vao
    out = array("h", bytes(2 * m))
    for j in range(m):
        x = j * vao / ra
        i = int(x)
        s0 = a[i]
        s1 = a[i + 1] if i + 1 < n else s0
        out[j] = int(s0 + (s1 - s0) * (x - i))
    return out.tobytes()


class Speaker:
    """``mo_phien()`` trả một phiên nói (``http_talk`` cổng 8086 của Imou, ``talk.TalkSession``
    cổng 37777, hay ``rtsp_talk.RtspTalkSession`` kênh ngược RTSP/ONVIF): dùng với ``with``, có
    ``send_pcm`` và ``tan_so``."""

    def __init__(self, hass: HomeAssistant, mo_phien: Callable[[], object]) -> None:
        self.hass = hass
        self._mo_phien = mo_phien
        self._lock = asyncio.Lock()
        self.playing = False
        #: ``time.monotonic()`` lúc gói tiếng CUỐI vừa gửi sang camera (``send_pcm`` gửi đúng
        #: nhịp thời gian thực nên đây là lúc tiếng dứt phía gửi) — chưa tính đóng phiên.
        self.het_tieng = 0.0

    @property
    def tan_so(self) -> int:
        """Tần số phiên sắp mở — nguồn tiếng nên sinh đúng tần số này (khỏi đổi hai lần)."""
        return int(getattr(self._mo_phien, "tan_so", TAN_SO))

    def _phien(self, hang: "queue.Queue[bytes | None]", vao: int) -> float:
        """Luồng executor: mở kênh nói, rút PCM ``vao`` Hz từ hàng đợi, đổi sang tần số của
        phiên nếu khác, rồi phát. Trả số giây."""
        giay = 0.0
        with self._mo_phien() as s:
            ra = int(getattr(s, "tan_so", TAN_SO))
            khoi = KHOI * ra // TAN_SO                  # 40 ms
            du = b""
            while (khuc := hang.get()) is not None:
                du += doi_tan_so(khuc, vao, ra)
                n = len(du) // khoi * khoi
                if n:
                    s.send_pcm(du[:n])
                    self.het_tieng = time.monotonic()
                    giay += n / (2 * ra)
                    du = du[n:]
            if du:
                s.send_pcm(du)
                self.het_tieng = time.monotonic()
                giay += len(du) / (2 * ra)
        return giay

    async def async_play_pcm(self, chunks: AsyncIterator[bytes], tan_so: int = TAN_SO) -> float:
        """Phát luồng PCM16 mono ``tan_so`` Hz. Trả số giây đã phát."""
        async with self._lock:
            hang: queue.Queue[bytes | None] = queue.Queue()
            self.playing = True
            viec = self.hass.async_add_executor_job(self._phien, hang, tan_so)
            try:
                try:
                    async for khuc in chunks:
                        hang.put(khuc)
                finally:
                    hang.put(None)          # luôn đóng phiên nói, kể cả khi nguồn hỏng
                return await viec
            finally:
                self.playing = False

    async def _ffmpeg_pcm(self, tan_so: int, args_vao: list[str], du_lieu: bytes | None = None
                          ) -> AsyncIterator[bytes]:
        """Đổi nguồn bất kỳ sang PCM16 mono ``tan_so`` Hz bằng ffmpeg của HA, nhả dần."""
        proc = await asyncio.create_subprocess_exec(
            get_ffmpeg_manager(self.hass).binary, "-nostdin", "-hide_banner",
            "-loglevel", "error", *args_vao,
            "-vn", "-ac", "1", "-ar", str(tan_so), "-f", "s16le", "pipe:",
            stdin=asyncio.subprocess.PIPE if du_lieu is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        if du_lieu is not None:
            proc.stdin.write(du_lieu)
            proc.stdin.close()
        try:
            while khuc := await proc.stdout.read(KHOI * 8):
                yield khuc
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    async def async_play_url(self, url: str) -> float:
        """Phát một URL (media source đã phân giải, tệp, luồng HTTP…)."""
        ts = self.tan_so
        return await self.async_play_pcm(self._ffmpeg_pcm(
            ts, ["-protocol_whitelist", "http,https,file,tcp,tls", "-i", url]), ts)

    async def async_play_wav(self, data: bytes) -> float:
        """Phát một tệp WAV. Đúng sẵn PCM16 mono đúng tần số loa thì khỏi qua ffmpeg."""
        ts = self.tan_so
        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                dung = (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (ts, 2, 1)
                pcm = w.readframes(w.getnframes()) if dung else b""
        except (wave.Error, EOFError):
            dung = False
        if dung:
            async def _mot():
                yield pcm
            return await self.async_play_pcm(_mot(), ts)
        return await self.async_play_pcm(self._ffmpeg_pcm(ts, ["-i", "pipe:"], data), ts)
