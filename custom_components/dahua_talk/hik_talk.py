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

Chương trình trợ giúp SỐNG GIỮA CÁC LƯỢT NÓI (giữ đăng nhập), mỗi lượt chỉ mở / đóng kênh đàm
thoại. Đo 29/09/2026 trên H6C: đăng nhập 0,6–1,3 s, đăng xuất 0,5 s, mở kênh 0,02–0,27 s. Đăng
nhập lại mỗi lượt thì tiếng bộ đàm dồn hàng đợi suốt lúc ấy và cả câu phát trễ theo. Ngồi yên
``NGHI_GIAY`` thì nó tự đăng xuất và thoát; lượt sau dựng lại.
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
#: Chờ chương trình trợ giúp đăng nhập / mở kênh đàm thoại tối đa ngần này giây.
_CHO_MO_GIAY = 15.0
#: ``send_pcm`` chỉ đi trước thời gian thực ngần này giây (mốc "ting dứt" dựa vào lúc nó trả về).
_DI_TRUOC = 0.15
_TOI_DA_GIAY = 300.0
#: Kênh đóng mà ngồi yên ngần này giây thì chương trình trợ giúp đăng xuất và thoát.
NGHI_GIAY = 60
#: Còn ngần này giây nữa là nó tự thoát thì thôi dùng lại — tránh gửi lệnh đúng lúc nó đang thoát.
_BIEN_NGHI = 5.0
#: Camera vừa đóng kênh thì chờ nó nhả kênh tối đa ngần này giây trước khi báo hỏng.
_CHO_NHA_KENH = 3.0
_MO_KENH = struct.pack(">I", 0xFFFFFFFF)
_DONG_KENH = struct.pack(">I", 0)


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


class TroGiup:
    """Một chương trình trợ giúp đang sống: đã đăng nhập camera, mở / đóng kênh theo lệnh.

    Dựng xong là đã đăng nhập (``ma``, ``tan_so`` = mã và tần số camera đòi); đăng nhập hỏng
    thì ném ``AuthError`` / ``TalkError``."""

    def __init__(self, host: str, port: int, username: str, password: str, sdk_dir: str) -> None:
        r, w = os.pipe()
        env = {**os.environ, "HIK_LIB": sdk_dir, "HIK_MK": password, "HIK_BAO_FD": str(w),
               "HIK_NGHI": str(NGHI_GIAY)}
        self._bao, self._du = r, b""
        try:
            self.p = subprocess.Popen(
                _lenh(host, port, username, sdk_dir), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, pass_fds=(w,))
        except BaseException:
            os.close(r)
            raise
        finally:
            os.close(w)
        #: ``time.monotonic()`` lúc kênh đóng lần gần nhất (mốc tính ngồi yên).
        self.ranh_tu = time.monotonic()
        dong = self._doc(_CHO_MO_GIAY)
        if not dong.startswith("SAN "):
            self.close()
            ma = re.search(r"mã (\d+)", dong)
            if "đăng nhập" in dong and ma and ma.group(1) == "1":
                raise AuthError("wrong password (HCNetSDK error 1)")
            raise TalkError(dong or "HCNetSDK helper did not answer")
        _san, self.ma, tan_so = dong.split()[:3]
        self.tan_so = int(tan_so)

    def _doc(self, cho: float) -> str:
        """Một dòng từ kênh báo; hết giờ hoặc chương trình đã thoát thì trả ""."""
        het = time.monotonic() + cho
        while b"\n" not in self._du:
            con = het - time.monotonic()
            if con <= 0 or not select.select([self._bao], [], [], con)[0]:
                return ""
            b = os.read(self._bao, 4096)
            if not b:
                return ""
            self._du += b
        dong, self._du = self._du.split(b"\n", 1)
        return dong.decode("utf-8", "replace").strip()

    def _gui(self, b: bytes) -> None:
        try:
            self.p.stdin.write(b)
            self.p.stdin.flush()
        except (OSError, ValueError) as exc:
            raise TalkError(f"HCNetSDK helper stopped ({exc})") from exc

    def dung_lai_duoc(self) -> bool:
        """Còn sống và còn xa lúc nó tự thoát vì ngồi yên."""
        return self.p.poll() is None and time.monotonic() - self.ranh_tu < NGHI_GIAY - _BIEN_NGHI

    def mo_kenh(self) -> None:
        """Mở kênh đàm thoại. Camera vừa đóng kênh chưa nhả xong thì chờ nó nhả rồi mở lại.

        Đo 29/09/2026 trên H6C: sau ``StopVoiceCom`` camera cần ~1,2 s mới nhả kênh — mở lại
        sau 0,2–1 s thì SDK tự chờ (259–1036 ms), mở ngay thì camera từ chối "(mã 29)" (thao tác
        thất bại). Bộ đàm gặp đúng cảnh ấy khi nói "alo, alo": câu sau mở kênh đúng lúc câu
        trước vừa đóng và bị mất."""
        het = time.monotonic() + _CHO_NHA_KENH
        while True:
            self._gui(_MO_KENH)
            dong = self._doc(_CHO_MO_GIAY)
            if dong == "OK":
                return
            ma = re.search(r"mã (\d+)", dong)
            if not (ma and ma.group(1) == "29" and time.monotonic() < het):
                raise TalkError(dong or "HCNetSDK helper did not answer")
            time.sleep(0.3)

    def dong_kenh(self, cho: float) -> bool:
        """Báo hết tiếng; nó phát nốt phần đệm rồi đóng kênh. Trả False nếu không đáp kịp."""
        try:
            self._gui(_DONG_KENH)
        except TalkError:
            return False
        xong = self._doc(cho) == "DONG"
        self.ranh_tu = time.monotonic()
        return xong

    def close(self) -> None:
        """Đóng stdin → nó đóng kênh (nếu đang mở), đăng xuất, thoát."""
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(3)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        if self._bao >= 0:
            os.close(self._bao)
            self._bao = -1


class HikTalkSession:
    """Một lượt nói qua chương trình trợ giúp đã đăng nhập: mở kênh, gửi tiếng, đóng kênh."""

    def __init__(self, tro_giup: TroGiup, *, ffmpeg: str = "ffmpeg") -> None:
        self.tg, self.ffmpeg = tro_giup, ffmpeg
        self.ma, self.tan_so = tro_giup.ma, tro_giup.tan_so
        self.hong = False                            # chương trình trợ giúp không đáp lúc đóng
        self._ff: subprocess.Popen | None = None
        self._luong: threading.Thread | None = None
        self._t_dau: float | None = None
        self._da_ghi = 0.0

    def __enter__(self) -> HikTalkSession:
        # ffmpeg khởi động song song với lúc camera mở kênh (mã đã biết từ lúc đăng nhập).
        ra = (["-c:a", "aac", "-b:a", "32k", "-f", "adts"] if self.ma == "AAC" else
              ["-c:a", "pcm_mulaw" if self.ma == "G711U" else "pcm_alaw",
               "-f", "mulaw" if self.ma == "G711U" else "alaw"])
        self._ff = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error",
             "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer",
             "-f", "s16le", "-ar", str(self.tan_so), "-ac", "1", "-i", "pipe:0",
             *ra, "-flush_packets", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.tg.mo_kenh()
        except BaseException:
            self._tat_ff()
            raise
        self._luong = threading.Thread(target=self._chuyen, args=(self._ff,),
                                       name="dahua-talk-hik", daemon=True)
        self._luong.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _chuyen(self, ff: subprocess.Popen) -> None:
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
                if khung:
                    self.tg._gui(b"".join(struct.pack(">I", len(k)) + k for k in khung))
        except (OSError, ValueError, TalkError):
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

    def _tat_ff(self) -> None:
        ff, self._ff = self._ff, None
        if ff is None:
            return
        try:
            ff.stdin.close()
        except OSError:
            pass
        if self._luong is not None:
            self._luong.join(_TOI_DA_GIAY)
        if ff.poll() is None:
            ff.kill()
        ff.wait()

    def close(self) -> None:
        if self._ff is None:
            return
        self._tat_ff()
        # Phần tiếng chưa phát còn tối đa cỡ ``_DI_TRUOC`` + bộ đệm ffmpeg, cộng 0,5 s đệm cuối.
        con = (self._t_dau or 0) + self._da_ghi - time.monotonic()
        self.hong = not self.tg.dong_kenh(max(0.0, con) + 5.0)


class _PhienHik:
    def __init__(self, mo: MoPhienHik) -> None:
        self._mo, self._s = mo, None

    def __enter__(self) -> HikTalkSession:
        self._s = self._mo._mo_luot()
        return self._s

    def __exit__(self, *_exc) -> None:
        if self._s is not None:
            self._s.close()
            if self._s.hong:
                self._mo.close()


class MoPhienHik:
    """Hàm mở phiên nói HCNetSDK cho ``Speaker``; giữ một chương trình trợ giúp đã đăng nhập
    giữa các lượt. ``tan_so`` = tần số camera báo (mặc định 16 kHz như H6C trước lần đầu)."""

    def __init__(self, host: str, username: str, password: str, *, sdk_dir: str,
                 ffmpeg: str = "ffmpeg", port: int = CONG_HIK) -> None:
        self.host, self.username, self.password = host, username, password
        self.sdk_dir, self.ffmpeg, self.port = sdk_dir, ffmpeg, port
        self.tan_so = 16000                          # EZVIZ H6C báo AAC 16 kHz
        self._tg: TroGiup | None = None
        self._khoa = threading.Lock()

    def __call__(self) -> _PhienHik:
        return _PhienHik(self)

    def _tro_giup(self) -> TroGiup:
        if self._tg is not None and not self._tg.dung_lai_duoc():
            self.close()
        if self._tg is None:
            self._tg = TroGiup(self.host, self.port, self.username, self.password, self.sdk_dir)
            self.tan_so = self._tg.tan_so            # lần sau loa sinh tiếng đúng tần số này
        return self._tg

    def _mo_luot(self) -> HikTalkSession:
        with self._khoa:
            moi = self._tg is None or not self._tg.dung_lai_duoc()
            tg = self._tro_giup()
            try:
                return HikTalkSession(tg, ffmpeg=self.ffmpeg).__enter__()
            except TalkError:
                if moi or tg.p.poll() is None:
                    raise                            # mới đăng nhập mà hỏng, hoặc camera từ chối
            # Bản đang giữ đã chết (camera khởi động lại…) — đăng nhập lại một lần.
            self.close()
            return HikTalkSession(self._tro_giup(), ffmpeg=self.ffmpeg).__enter__()

    def close(self) -> None:
        tg, self._tg = self._tg, None
        if tg is not None:
            tg.close()


def check_hik_talk(host: str, username: str, password: str, sdk_dir: str,
                   port: int = CONG_HIK) -> str:
    """Đăng nhập + mở rồi đóng ngay kênh đàm thoại — KHÔNG phát gì. Trả mã âm thanh camera đòi."""
    tg = TroGiup(host, port, username, password, sdk_dir)
    try:
        tg.mo_kenh()
        tg.dong_kenh(5.0)
        return tg.ma
    finally:
        tg.close()


__all__ = ["CONG_HIK", "HikTalkSession", "MoPhienHik", "TroGiup", "check_hik_talk",
           "sdk_san_sang"]
