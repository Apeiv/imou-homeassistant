"""Tự tải HCNetSDK (Hikvision Device Network SDK) cho EZVIZ — người dùng khỏi chép tay.

Trang tải của Hikvision chặn tải tự động (HTTP 403 cho mọi yêu cầu không qua trình duyệt, đo
29/09/2026), nên gói ``lib`` được đặt ở repo RIÊNG ``TriTue2011/hcnetsdk-linux`` (phần Release) —
tách khỏi repo tích hợp. Gói nào cũng ghim mã sha256: tải về lệch một byte là bỏ, không giải nén.

Chỉ tải khi thêm / cấu hình lại camera EZVIZ mà ``/config/hcnetsdk/lib`` chưa có SDK. Hỏng thì
tích hợp đi đường cũ (kênh ngược RTSP) và ghi cảnh báo — không chặn việc thêm camera.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import HIK_SDK_DIR
from .hik_talk import kien_truc, sdk_san_sang

_LOGGER = logging.getLogger(__name__)

#: Kiến trúc → (URL gói, sha256). Chỉ những máy có cả SDK lẫn bản trợ giúp ``hik/hik_noi-<kt>``.
GOI: dict[str, tuple[str, str]] = {
    "x86_64": ("https://github.com/TriTue2011/hcnetsdk-linux/releases/download/v6.1.9.4/"
               "hcnetsdk-linux-x86_64-V6.1.9.4.tar.gz",
               "a75e059f0a9aea5c7a0b52e63c513773799da3980c52e8dd29239b214df2881a"),
}
#: Gói thật ~10 MB; lớn hơn hẳn là tải nhầm thứ khác.
_TOI_DA = 64 * 1024 * 1024


class SdkTaiLoi(Exception):
    """Không tải / không kiểm / không giải nén được gói SDK."""


def giai_nen(du_lieu: bytes, sha256: str, sdk_dir: Path) -> None:
    """Kiểm sha256 rồi giải gói (thư mục ``lib/`` ở gốc) thành ``sdk_dir``. Giải vào thư mục tạm
    cạnh đó rồi mới đổi tên — hỏng giữa chừng không để lại ``lib`` dở dang."""
    if hashlib.sha256(du_lieu).hexdigest() != sha256:
        raise SdkTaiLoi("sha256 mismatch — refusing to unpack")
    tam = sdk_dir.parent / ".tai"
    shutil.rmtree(tam, ignore_errors=True)
    tam.mkdir(parents=True)
    try:
        import io
        with tarfile.open(fileobj=io.BytesIO(du_lieu), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not (m.name == "lib" or m.name.startswith("lib/")) or ".." in Path(m.name).parts \
                        or not (m.isfile() or m.isdir()):
                    raise SdkTaiLoi(f"unexpected entry in package: {m.name}")
            tf.extractall(tam, filter="data")
        if not (tam / "lib" / "libhcnetsdk.so").is_file():
            raise SdkTaiLoi("package has no lib/libhcnetsdk.so")
        if sdk_dir.exists():
            shutil.rmtree(sdk_dir)
        (tam / "lib").rename(sdk_dir)
    finally:
        shutil.rmtree(tam, ignore_errors=True)


async def async_dam_bao_sdk(hass: HomeAssistant) -> bool:
    """Có SDK dùng được chưa; chưa có mà máy này có gói thì tải về. True = nói qua SDK được."""
    sdk_dir = Path(hass.config.path(HIK_SDK_DIR))
    if sdk_san_sang(str(sdk_dir)):
        return True
    goi = GOI.get(kien_truc())
    if goi is None:
        _LOGGER.warning("HCNetSDK: no package for this machine (%s) — EZVIZ uses the RTSP "
                        "backchannel; copy the SDK lib folder to %s by hand", kien_truc(), sdk_dir)
        return False
    url, sha = goi
    try:
        async with async_get_clientsession(hass).get(url, timeout=120) as r:
            if r.status != 200:
                raise SdkTaiLoi(f"HTTP {r.status}")
            # Đọc TỪNG KHÚC tới hết: ``content.read(n)`` chỉ trả phần đang có trong bộ đệm (gặp thật
            # 29/09/2026 trên HA OS: nhận thiếu gói → "sha256 mismatch").
            du_lieu = bytearray()
            async for khoi in r.content.iter_chunked(1 << 16):
                du_lieu += khoi
                if len(du_lieu) > _TOI_DA:
                    raise SdkTaiLoi("package too large")
        await hass.async_add_executor_job(giai_nen, bytes(du_lieu), sha, sdk_dir)
    except (SdkTaiLoi, OSError, tarfile.TarError, TimeoutError) as exc:
        _LOGGER.warning("HCNetSDK download from %s failed: %s — EZVIZ uses the RTSP backchannel",
                        url, exc)
        return False
    except Exception as exc:  # noqa: BLE001 — lỗi mạng aiohttp: không được chặn việc thêm camera
        _LOGGER.warning("HCNetSDK download from %s failed: %s", url, exc)
        return False
    _LOGGER.info("HCNetSDK downloaded to %s", sdk_dir)
    return sdk_san_sang(str(sdk_dir))
