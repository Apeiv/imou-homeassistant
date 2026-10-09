"""Nói ra loa camera Hikvision / EZVIZ qua HCNetSDK (cổng thiết bị 8000).

Python thuần phía HA — cùng giao diện với ``talk.TalkSession`` (``with``, ``send_pcm``, ``close``,
``tan_so``). Vì sao cần: EZVIZ H6C (đo 28/09/2026) KHÔNG có đường nói nào khác — RTSP không có
kênh ngược, không ONVIF, cổng HTTP/ISAPI bị khoá; có đời EZVIZ nhận kênh ngược RTSP nhưng loa
câm (issue #1). HCNetSDK qua cổng 8000 thì mở được kênh đàm thoại và loa phát thật.

HCNetSDK là thư viện dựng cho glibc, còn container HA (cả HA OS) chạy Alpine (musl) — không nạp
được trực tiếp. Tích hợp mang theo chương trình ``hik/hik_noi-<kiến trúc>`` (mã nguồn
``hik/hik_noi.c``) và bộ glibc nhỏ ``hik/glibc-<kiến trúc>``; chạy qua trình nạp glibc đi kèm
(đã thử trong container HA Alpine: chạy được). Bản thân HCNetSDK KHÔNG nằm trong repo này (bản
quyền Hikvision): ``sdk_tai`` tự tải gói ``lib`` vào ``/config/hcnetsdk/lib`` từ repo riêng
``TriTue2011/hcnetsdk-linux``, hoặc người dùng tự chép.

Luồng: PCM16 ``tan_so`` → ffmpeg của HA mã hoá theo mã camera đòi (AAC ADTS / G.711) → khung
→ chương trình trợ giúp (giữ nhịp thời gian thực) → ``NET_DVR_VoiceComSendData``.

Chương trình trợ giúp SỐNG GIỮA CÁC LƯỢT NÓI (giữ đăng nhập), mỗi lượt chỉ mở / đóng kênh đàm
thoại. Đo 29/09/2026 trên H6C: đăng nhập 0,6–1,3 s, đăng xuất 0,5 s, mở kênh 0,02–0,27 s. Đăng
nhập lại mỗi lượt thì tiếng bộ đàm dồn hàng đợi suốt lúc ấy và cả câu phát trễ theo. Ngồi yên
``NGHI_GIAY`` (cả tuần: thực tế là không bao giờ) thì nó tự đăng xuất và thoát; lượt sau dựng lại.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import select
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .http_talk import cat_adts
from .talk import FFMPEG_TRUC_TIEP, TAN_SO, AuthError, TalkError

_LOGGER = logging.getLogger(__name__)

CONG_HIK = 8000
_THU_MUC = Path(__file__).parent / "hik"
#: Chờ chương trình trợ giúp đăng nhập / mở kênh đàm thoại tối đa ngần này giây.
_CHO_MO_GIAY = 15.0
#: ``send_pcm`` chỉ đi trước thời gian thực ngần này giây (mốc "ting dứt" dựa vào lúc nó trả về).
_DI_TRUOC = 0.15
_TOI_DA_GIAY = 300.0
#: Kênh đóng mà ngồi yên ngần này giây thì chương trình trợ giúp đăng xuất và thoát. Đo DB1C 09/10/2026: dựng lại
#: từ đầu 8,6 s, còn đăng nhập 0,8 s → giữ cả tuần (hik_noi.c nhận mili-giây trong ``int``: 604 800 000 < 2^31).
NGHI_GIAY = 7 * 24 * 3600
#: Còn ngần này giây nữa là nó tự thoát thì thôi dùng lại — tránh gửi lệnh đúng lúc nó đang thoát.
_BIEN_NGHI = 5.0
#: Camera vừa đóng kênh thì chờ nó nhả kênh tối đa ngần này giây trước khi báo hỏng.
_CHO_NHA_KENH = 3.0
#: Mã lỗi HCNetSDK trong dòng «LOI <mã> …» của hik_noi.c (NET_DVR_GetLastError).
_MA_SAI_MAT_KHAU = 1                         # NET_DVR_PASSWORD_ERROR
_MA_BAN = 29                                 # NET_DVR_DVROPRATEFAILED: camera bận / chưa nhả kênh
#: «Bận» (``_MA_BAN``) trong ngần này giây sau lượt vừa nói là camera bận thật: báo lỗi, không đăng nhập lại.
_VUA_NOI_GIAY = 60.0
_MO_KENH = struct.pack(">I", 0xFFFFFFFF)
_DONG_KENH = struct.pack(">I", 0)


def _dinh_dang(ma: str) -> str:
    """Tên định dạng ffmpeg của mã camera đòi (``SAN <mã>``): aac (ADTS) / mulaw / alaw."""
    return "aac" if ma == "AAC" else "mulaw" if ma == "G711U" else "alaw"


def ma_loi(dong: str) -> int | None:
    """Mã lỗi SDK của dòng «LOI <mã> …» (0 = không phải lỗi SDK); dòng khác thì None."""
    m = re.match(r"LOI (\d+) ", dong)
    return int(m.group(1)) if m else None


class CameraBan(TalkError):
    """Camera từ chối mở kênh đàm thoại vì bận (``_MA_BAN``) quá ``_CHO_NHA_KENH`` giây."""


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

    def __init__(self, host: str, port: int, username: str, password: str, sdk_dir: str, *,
                 nghe: Callable[[bytes], None] | None = None, ffmpeg: str = "ffmpeg") -> None:
        r, w = os.pipe()
        # ``nghe``: nhận PCM16 mono ``TAN_SO`` Hz tiếng MIC CAMERA trong lúc kênh đàm thoại mở (đàm thoại hai
        # chiều) — SDK đưa khung mã hoá về qua HIK_NGHE_FD, ffmpeg giải mã, luồng riêng gọi ``nghe`` từng khúc.
        nghe_r, nghe_w = os.pipe() if nghe else (-1, -1)
        env = {**os.environ, "HIK_LIB": sdk_dir, "HIK_MK": password, "HIK_BAO_FD": str(w),
               "HIK_NGHI": str(NGHI_GIAY), **({"HIK_NGHE_FD": str(nghe_w)} if nghe else {})}
        self._bao, self._du, self._nghe_r, self._ff_nghe = r, b"", nghe_r, None
        try:
            self.p = subprocess.Popen(
                _lenh(host, port, username, sdk_dir), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                pass_fds=(w, nghe_w) if nghe else (w,))
        except BaseException:
            os.close(r)
            self._dong_nghe()
            raise
        finally:
            os.close(w)
            if nghe_w >= 0:
                os.close(nghe_w)
        #: ``time.monotonic()`` lúc kênh đóng lần gần nhất (mốc tính ngồi yên).
        self.ranh_tu = time.monotonic()
        # Thu dọn ngay khi nó tự thoát vì ngồi yên (issue #2: camera lâu không nói để lại tiến trình <defunct>).
        threading.Thread(target=self.p.wait, name="dahua-talk-hik-don", daemon=True).start()
        dong = self._doc(_CHO_MO_GIAY)
        if not dong.startswith("SAN "):
            self.close()
            if ma_loi(dong) == _MA_SAI_MAT_KHAU:      # dòng LOI trước SAN là lỗi đăng nhập
                raise AuthError("wrong password (HCNetSDK error 1)")
            raise TalkError(dong or "HCNetSDK helper did not answer")
        _san, self.ma, tan_so = dong.split()[:3]
        self.tan_so = int(tan_so)
        if nghe:
            try:
                self._mo_nghe(nghe, ffmpeg)
            except BaseException:
                self.close()
                raise

    def _mo_nghe(self, nghe: Callable[[bytes], None], ffmpeg: str) -> None:
        """Khung mã hoá từ HIK_NGHE_FD → ffmpeg (đọc thẳng ống dẫn) → PCM16 mono ``TAN_SO`` Hz → ``nghe``."""
        # ponytail: 8 kHz cố định — thẻ phát lại bằng bộ phát G.711 của card Vimar; đổi khi cần băng rộng.
        ff = subprocess.Popen(
            [ffmpeg, *FFMPEG_TRUC_TIEP, "-f", _dinh_dang(self.ma), "-i", "pipe:0",
             "-f", "s16le", "-ar", str(TAN_SO), "-ac", "1", "-flush_packets", "1", "pipe:1"],
            stdin=self._nghe_r, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._dong_nghe()                            # ffmpeg giữ đầu đọc; hết chương trình trợ giúp là hết ống

        def chep() -> None:
            while khuc := ff.stdout.read1(4096):
                nghe(khuc)
            _LOGGER.debug("listen decoder exited %s", ff.wait())

        threading.Thread(target=chep, name="dahua-talk-hik-nghe", daemon=True).start()
        self._ff_nghe = ff

    def _dong_nghe(self) -> None:
        if self._nghe_r >= 0:
            os.close(self._nghe_r)
            self._nghe_r = -1

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

    def loi_giua_luot(self) -> str:
        """Đọc KHÔNG CHẶN kênh báo giữa lượt nói: trả dòng «LOI …» nếu chương trình trợ giúp vừa báo (gửi tiếng hỏng,
        camera rớt kênh đàm thoại), không thì "". Dòng khác giữ lại cho ``_doc``."""
        while select.select([self._bao], [], [], 0)[0]:
            b = os.read(self._bao, 4096)
            if not b:
                return "LOI HCNetSDK helper exited"
            self._du += b
        while b"\n" in self._du:
            dong, _, con = self._du.partition(b"\n")
            chu = dong.decode("utf-8", "replace").strip()
            if chu.startswith("LOI"):
                self._du = con
                return chu
            break
        return ""

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
        sau 0,2–1 s thì SDK tự chờ (259–1036 ms), mở ngay thì camera từ chối ``_MA_BAN`` (thao tác
        thất bại). Bộ đàm gặp đúng cảnh ấy khi nói "alo, alo": câu sau mở kênh đúng lúc câu
        trước vừa đóng và bị mất."""
        het = time.monotonic() + _CHO_NHA_KENH
        while True:
            self._gui(_MO_KENH)
            dong = self._doc(_CHO_MO_GIAY)
            if dong == "OK":
                return
            if ma_loi(dong) != _MA_BAN:
                raise TalkError(dong or "HCNetSDK helper did not answer")
            if time.monotonic() >= het:
                raise CameraBan(dong)
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
        self._dong_nghe()
        if self._ff_nghe is not None:                # luồng ``chep`` thu dọn (wait) sau khi nó thoát
            self._ff_nghe.kill()
            self._ff_nghe = None


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
        #: Lỗi luồng chuyển khung (chương trình trợ giúp chết…) — ``send_pcm`` ném ra thay vì kẹt ở stdin ffmpeg.
        self._loi: BaseException | None = None

    def __enter__(self) -> HikTalkSession:
        # ffmpeg khởi động song song với lúc camera mở kênh (mã đã biết từ lúc đăng nhập).
        ra = (["-c:a", "aac", "-b:a", "32k", "-f", "adts"] if self.ma == "AAC" else
              ["-c:a", "pcm_" + _dinh_dang(self.ma), "-f", _dinh_dang(self.ma)])
        self._ff = subprocess.Popen(
            [self.ffmpeg, *FFMPEG_TRUC_TIEP,
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
        except (OSError, ValueError, TalkError) as exc:
            # Issue #2: trước đây thoát LẶNG — không ai đọc ffmpeg, ống đầy, ``send_pcm`` kẹt ở stdin.write (cùng lớp
            # với lỗi 8086 đã sửa ở 0.9.0). Ghi lỗi và tắt ffmpeg để ``send_pcm`` báo lỗi ngay.
            self._loi = exc
            if ff.poll() is None:
                ff.kill()

    def send_pcm(self, pcm: bytes) -> None:
        """PCM16 LE mono ``tan_so`` Hz. Trả về khi tiếng sắp phát xong (như ``TalkSession``).

        Camera rớt mạng giữa bài: chương trình trợ giúp báo «LOI …» (issue #2 — trước đây nó bỏ qua kết quả
        ``NET_DVR_VoiceComSendData`` và loa «đang phát» 44 phút vào kết nối chết) → ném ``TalkError`` để
        media_player tự nối lại / dừng."""
        if self._loi is not None:
            raise TalkError(f"HCNetSDK helper stopped taking audio ({self._loi})")
        if loi := self.tg.loi_giua_luot():
            raise TalkError(loi)
        if self._t_dau is None:
            self._t_dau = time.monotonic()
        try:
            self._ff.stdin.write(pcm)
            self._ff.stdin.flush()
        except (BrokenPipeError, ValueError, AttributeError) as exc:
            raise TalkError(f"audio encoder stopped ({self._loi or exc})") from exc
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
                self._mo._bo_tro_giup()


class MoPhienHik:
    """Hàm mở phiên nói HCNetSDK cho ``Speaker``; giữ một chương trình trợ giúp đã đăng nhập
    giữa các lượt. ``tan_so`` = tần số camera báo (mặc định 16 kHz như H6C trước lần đầu)."""

    def __init__(self, host: str, username: str, password: str, *, sdk_dir: str,
                 ffmpeg: str = "ffmpeg", port: int = CONG_HIK, nghe=None) -> None:
        self.host, self.username, self.password = host, username, password
        self.sdk_dir, self.ffmpeg, self.port = sdk_dir, ffmpeg, port
        #: ``intercom.Nghe`` (``feed(pcm)`` + ``co_nguoi_nghe``): tiếng mic camera về trong lúc kênh mở đi đâu.
        self.nghe = nghe
        self._da_dong = False                        # mục đã gỡ: không dựng chương trình trợ giúp nữa
        self.tan_so = 16000                          # EZVIZ H6C báo AAC 16 kHz
        self._tg: TroGiup | None = None
        self._khoa = threading.RLock()               # ``_mo_luot`` gọi ``close()`` khi đang giữ khoá

    @property
    def hai_chieu(self) -> bool:
        """Đang có thẻ nghe tiếng camera về → bộ đàm giữ kênh mở suốt lượt (không VOX)."""
        return self.nghe is not None and self.nghe.co_nguoi_nghe

    def __call__(self) -> _PhienHik:
        return _PhienHik(self)

    def _tro_giup(self) -> TroGiup:
        if self._da_dong:
            # Gỡ mục trước khi lượt đăng nhập sẵn kịp chạy: đừng dựng chương trình trợ giúp mồ côi (đăng nhập tới khi
            # ``NGHI_GIAY`` hết, giữ mật khẩu, không ai đóng).
            raise TalkError("integration entry unloaded")
        if self._tg is not None and not self._tg.dung_lai_duoc():
            self._bo_tro_giup()
        if self._tg is None:
            self._tg = TroGiup(self.host, self.port, self.username, self.password, self.sdk_dir,
                               nghe=self.nghe.feed if self.nghe else None, ffmpeg=self.ffmpeg)
            self.tan_so = self._tg.tan_so            # lần sau loa sinh tiếng đúng tần số này
        return self._tg

    def dang_nhap_truoc(self) -> None:
        """Đăng nhập sẵn lúc dựng mục (xem ``NGHI_GIAY``); hỏng thì chỉ ghi log, lượt nói sẽ báo lỗi đúng chỗ."""
        with self._khoa:
            try:
                self._tro_giup()
            except Exception as exc:  # noqa: BLE001 — chạy nền, không ai đón
                _LOGGER.warning("HCNetSDK: pre-login failed: %s", exc)

    def _mo_luot(self) -> HikTalkSession:
        with self._khoa:
            moi = self._tg is None or not self._tg.dung_lai_duoc()
            t0 = time.monotonic()
            tg = self._tro_giup()
            try:
                s = HikTalkSession(tg, ffmpeg=self.ffmpeg).__enter__()
            except TalkError as exc:
                # Mới đăng nhập mà hỏng, hay camera bận ngay sau một lượt vừa nói: camera từ chối thật.
                # Còn lại là bản đang giữ hỏng (chết, phiên SDK cũ sau khi camera khởi động lại): đăng nhập lại một lần.
                if moi or (isinstance(exc, CameraBan) and time.monotonic() - tg.ranh_tu < _VUA_NOI_GIAY):
                    raise
                self._bo_tro_giup()
                s = HikTalkSession(self._tro_giup(), ffmpeg=self.ffmpeg).__enter__()
            _LOGGER.debug("HCNetSDK: turn ready in %.1f s (%s)", time.monotonic() - t0, "new login" if moi else "reused")
            return s

    def close(self) -> None:
        """Gỡ mục: đóng chương trình trợ giúp và không dựng lại nữa."""
        with self._khoa:                             # gỡ mục giữa lúc đăng nhập sẵn: chờ xong rồi đóng, không bỏ sót
            self._da_dong = True
            self._bo_tro_giup()

    def _bo_tro_giup(self) -> None:
        """Bỏ chương trình trợ giúp đang giữ (hỏng / hết hạn); lượt sau dựng lại."""
        with self._khoa:
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


__all__ = ["CONG_HIK", "CameraBan", "HikTalkSession", "MoPhienHik", "TroGiup", "check_hik_talk",
           "sdk_san_sang"]
