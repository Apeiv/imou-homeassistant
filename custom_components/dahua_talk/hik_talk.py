"""Nói ra loa camera Hikvision / EZVIZ qua HCNetSDK (cổng thiết bị 8000).

Python thuần phía HA — cùng giao diện với ``talk.TalkSession`` (``with``, ``send_pcm``, ``close``,
``tan_so``). Vì sao cần: EZVIZ H6C (đo 28/09/2026) KHÔNG có đường nói nào khác — RTSP không có
kênh ngược, không ONVIF, cổng HTTP/ISAPI bị khoá; có đời EZVIZ nhận kênh ngược RTSP nhưng loa
câm (issue #1). HCNetSDK qua cổng 8000 thì mở được kênh đàm thoại và loa phát thật.

HCNetSDK là thư viện dựng cho glibc, còn container HA (cả HA OS) chạy Alpine (musl) — không nạp
được trực tiếp. Tích hợp mang theo chương trình ``hik/hik_noi-<kiến trúc>`` (mã nguồn
``hik/hik_noi.c``) và bộ glibc nhỏ ``hik/glibc-<kiến trúc>``; chạy qua trình nạp glibc đi kèm
(đã thử trong container HA Alpine: chạy được). Bản thân HCNetSDK KHÔNG đi kèm (bản quyền
Hikvision): người dùng tải "Device Network SDK (Linux 64-bit)" và chép thư mục ``lib`` vào
``/config/hcnetsdk/lib``.

Luồng: PCM16 ``tan_so`` → ffmpeg của HA mã hoá theo mã camera đòi (AAC ADTS / G.711) → khung
→ chương trình trợ giúp (giữ nhịp thời gian thực) → ``NET_DVR_VoiceComSendData``.
"""

from __future__ import annotations

import os
import platform
import re
import select
import struct
import subprocess
import threading
import time
from pathlib import Path

from .http_talk import cat_adts
from .talk import AuthError, TalkError

CONG_HIK = 8000
_THU_MUC = Path(__file__).parent / "hik"
#: Chờ chương trình trợ giúp đăng nhập + mở kênh đàm thoại tối đa ngần này giây.
_CHO_MO_GIAY = 15.0
#: ``send_pcm`` chỉ đi trước thời gian thực ngần này giây (mốc "ting dứt" dựa vào lúc nó trả về).
_DI_TRUOC = 0.15
_TOI_DA_GIAY = 300.0


def kien_truc() -> str:
    return platform.machine() or "x86_64"


def sdk_san_sang(sdk_dir: str) -> bool:
    """Đủ để nói qua HCNetSDK: có SDK người dùng chép vào và có bản trợ giúp cho máy này."""
    return (Path(sdk_dir) / "libhcnetsdk.so").is_file() and (
        _THU_MUC / f"hik_noi-{kien_truc()}").is_file()


def _lenh(host: str, port: int, username: str, sdk_dir: str) -> list[str]:
    ak = kien_truc()
    glibc = _THU_MUC / f"glibc-{ak}"
    tro_giup = _THU_MUC / f"hik_noi-{ak}"
    nap = next(glibc.glob("ld-linux*.so*"), None)
    if nap is None or not tro_giup.is_file():
        raise TalkError(f"no HCNetSDK helper for this machine ({ak})")
    for tep in (nap, tro_giup):
        # HACS / giải nén có thể làm mất bit chạy — đặt lại thay vì báo "Permission denied".
        if not os.access(tep, os.X_OK):
            tep.chmod(tep.stat().st_mode | 0o111)
    thu_vien = ":".join([str(glibc), sdk_dir, str(Path(sdk_dir) / "HCNetSDKCom")])
    return [str(nap), "--library-path", thu_vien, str(tro_giup), host, str(port), username]


class HikTalkSession:
    """Một phiên nói qua HCNetSDK. ``tan_so`` biết sau khi mở (camera báo mã âm thanh)."""

    def __init__(self, host: str, username: str, password: str, *, sdk_dir: str,
                 ffmpeg: str = "ffmpeg", port: int = CONG_HIK) -> None:
        self.host, self.username, self.password = host, username, password
        self.sdk_dir, self.ffmpeg, self.port = sdk_dir, ffmpeg, port
        self.ma = ""
        self.tan_so = 16000
        self._tro_giup: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None
        self._luong: threading.Thread | None = None
        self._t_dau: float | None = None
        self._da_ghi = 0.0

    def __enter__(self) -> HikTalkSession:
        try:
            self._mo()
        except BaseException:
            self.close(cho=False)
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _mo_tro_giup(self) -> str:
        """Chạy chương trình trợ giúp; trả dòng "OK <mã> <tần_số>" hoặc ném lỗi."""
        r, w = os.pipe()
        env = {**os.environ, "HIK_LIB": self.sdk_dir, "HIK_MK": self.password,
               "HIK_BAO_FD": str(w)}
        try:
            self._tro_giup = subprocess.Popen(
                _lenh(self.host, self.port, self.username, self.sdk_dir), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, pass_fds=(w,))
        finally:
            os.close(w)
        with os.fdopen(r, "rb") as bao:
            san, _w, _x = select.select([bao], [], [], _CHO_MO_GIAY)
            dong = bao.readline().decode("utf-8", "replace").strip() if san else ""
        if not dong.startswith("OK "):
            ma = re.search(r"mã (\d+)", dong)
            if "đăng nhập" in dong and ma and ma.group(1) == "1":
                raise AuthError("wrong password (HCNetSDK error 1)")
            raise TalkError(dong or "HCNetSDK helper did not answer")
        return dong

    def _mo(self) -> None:
        _ok, self.ma, tan_so = self._mo_tro_giup().split()[:3]
        self.tan_so = int(tan_so)
        ra = (["-c:a", "aac", "-b:a", "32k", "-f", "adts"] if self.ma == "AAC" else
              ["-c:a", "pcm_mulaw" if self.ma == "G711U" else "pcm_alaw",
               "-f", "mulaw" if self.ma == "G711U" else "alaw"])
        self._ff = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error",
             "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer",
             "-f", "s16le", "-ar", str(self.tan_so), "-ac", "1", "-i", "pipe:0",
             *ra, "-flush_packets", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._luong = threading.Thread(target=self._chuyen, args=(self._ff, self._tro_giup),
                                       name="dahua-talk-hik", daemon=True)
        self._luong.start()

    def _chuyen(self, ff: subprocess.Popen, tro_giup: subprocess.Popen) -> None:
        """Khung mã hoá từ ffmpeg → chương trình trợ giúp (4 byte độ dài + dữ liệu)."""
        du = b""
        try:
            while b := ff.stdout.read1(4096):
                if self.ma == "AAC":
                    khung, du = cat_adts(du + b)
                else:
                    du += b
                    n = len(du) // 160 * 160
                    khung = [du[i:i + 160] for i in range(0, n, 160)]
                    du = du[n:]
                for k in khung:
                    tro_giup.stdin.write(struct.pack(">I", len(k)) + k)
                tro_giup.stdin.flush()
            tro_giup.stdin.write(struct.pack(">I", 0))
            tro_giup.stdin.close()
        except (OSError, ValueError):
            pass

    def send_pcm(self, pcm: bytes) -> None:
        """PCM16 LE mono ``tan_so`` Hz. Trả về khi tiếng sắp phát xong (như ``TalkSession``)."""
        if self._t_dau is None:
            self._t_dau = time.monotonic()
        try:
            self._ff.stdin.write(pcm)
            self._ff.stdin.flush()
        except (BrokenPipeError, ValueError, AttributeError) as exc:
            raise TalkError(f"audio encoder stopped ({exc})") from exc
        self._da_ghi += len(pcm) / (2 * self.tan_so)
        cho = self._t_dau + self._da_ghi - _DI_TRUOC - time.monotonic()
        if cho > 0:
            time.sleep(cho)

    def close(self, cho: bool = True) -> None:
        ff, self._ff = self._ff, None
        if ff is not None:
            try:
                ff.stdin.close()
            except OSError:
                pass
            if cho and self._luong is not None:
                self._luong.join(_TOI_DA_GIAY)
            if ff.poll() is None:
                ff.kill()
            ff.wait()
        tg, self._tro_giup = self._tro_giup, None
        if tg is not None:
            try:
                if tg.stdin and not tg.stdin.closed:
                    tg.stdin.write(struct.pack(">I", 0))
                    tg.stdin.close()
            except OSError:
                pass
            try:
                tg.wait(_TOI_DA_GIAY if cho else 2)
            except subprocess.TimeoutExpired:
                tg.kill()
                tg.wait()


class _PhienHik:
    def __init__(self, mo: MoPhienHik) -> None:
        self._mo, self._s = mo, None

    def __enter__(self) -> HikTalkSession:
        self._s = HikTalkSession(self._mo.host, self._mo.username, self._mo.password,
                                 sdk_dir=self._mo.sdk_dir, ffmpeg=self._mo.ffmpeg,
                                 port=self._mo.port).__enter__()
        self._mo.tan_so = self._s.tan_so              # lần sau loa sinh tiếng đúng tần số này
        return self._s

    def __exit__(self, *_exc) -> None:
        if self._s is not None:
            self._s.close()


class MoPhienHik:
    """Hàm mở phiên nói HCNetSDK cho ``Speaker``. ``tan_so`` = tần số lần mở gần nhất."""

    def __init__(self, host: str, username: str, password: str, *, sdk_dir: str,
                 ffmpeg: str = "ffmpeg", port: int = CONG_HIK) -> None:
        self.host, self.username, self.password = host, username, password
        self.sdk_dir, self.ffmpeg, self.port = sdk_dir, ffmpeg, port
        self.tan_so = 16000                          # EZVIZ H6C báo AAC 16 kHz

    def __call__(self) -> _PhienHik:
        return _PhienHik(self)


def check_hik_talk(host: str, username: str, password: str, sdk_dir: str,
                   port: int = CONG_HIK) -> str:
    """Đăng nhập + mở rồi đóng ngay kênh đàm thoại — KHÔNG phát gì. Trả mã âm thanh camera đòi."""
    s = HikTalkSession(host, username, password, sdk_dir=sdk_dir, port=port)
    try:
        return s._mo_tro_giup().split()[1]
    finally:
        s.close(cho=False)


__all__ = ["CONG_HIK", "HikTalkSession", "MoPhienHik", "check_hik_talk", "sdk_san_sang"]
