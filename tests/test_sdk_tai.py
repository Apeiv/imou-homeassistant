"""Tự tải HCNetSDK cho EZVIZ: kiểm sha256, chỉ giải thư mục ``lib/``, hỏng thì đi đường RTSP."""

import hashlib
import io
import tarfile
from pathlib import Path
from unittest import mock

import pytest

from custom_components.dahua_talk import sdk_tai
from custom_components.dahua_talk.const import HIK_SDK_DIR


def _goi(tep: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as tf:
        for ten, noi in tep.items():
            ti = tarfile.TarInfo(ten)
            ti.size = len(noi)
            tf.addfile(ti, io.BytesIO(noi))
    return b.getvalue()


GOI_DUNG = _goi({"lib/libhcnetsdk.so": b"so", "lib/HCNetSDKCom/libHCVoiceTalk.so": b"vt"})


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_giai_nen_dung_goi(tmp_path):
    dich = tmp_path / "hcnetsdk" / "lib"
    dich.mkdir(parents=True)
    (dich / "cu.so").write_bytes(b"cu")                  # lib dở dang cũ bị thay hẳn
    sdk_tai.giai_nen(GOI_DUNG, _sha(GOI_DUNG), dich)
    assert (dich / "libhcnetsdk.so").read_bytes() == b"so"
    assert (dich / "HCNetSDKCom" / "libHCVoiceTalk.so").is_file()
    assert not (dich / "cu.so").exists() and not (tmp_path / "hcnetsdk" / ".tai").exists()


@pytest.mark.parametrize("goi, sha, loi", [
    (GOI_DUNG, "0" * 64, "sha256"),
    (_goi({"lib/libhcnetsdk.so": b"so", "../ngoai.so": b"x"}), None, "unexpected"),
    (_goi({"khac/libhcnetsdk.so": b"so"}), None, "unexpected"),
    (_goi({"lib/libkhac.so": b"so"}), None, "no lib/libhcnetsdk.so"),
])
def test_giai_nen_tu_choi(tmp_path, goi, sha, loi):
    dich = tmp_path / "hcnetsdk" / "lib"
    with pytest.raises(sdk_tai.SdkTaiLoi, match=loi):
        sdk_tai.giai_nen(goi, sha or _sha(goi), dich)
    assert not dich.exists() and not (tmp_path / "ngoai.so").exists()


async def test_tai_khi_chua_co_roi_bao_san_sang(hass, aioclient_mock, tmp_path):
    url = "https://example.test/sdk.tar.gz"
    aioclient_mock.get(url, content=GOI_DUNG)
    with mock.patch.dict(sdk_tai.GOI, {"x86_64": (url, _sha(GOI_DUNG))}, clear=True), \
            mock.patch.object(sdk_tai, "kien_truc", return_value="x86_64"), \
            mock.patch.object(hass.config, "path", side_effect=lambda *p: str(tmp_path.joinpath(*p))), \
            mock.patch.object(sdk_tai, "sdk_san_sang", side_effect=lambda d: (Path(d) / "libhcnetsdk.so").is_file()):
        assert await sdk_tai.async_dam_bao_sdk(hass) is True
        assert (tmp_path / HIK_SDK_DIR / "libhcnetsdk.so").is_file()
        assert await sdk_tai.async_dam_bao_sdk(hass) is True          # có rồi thì không tải lại
    assert aioclient_mock.call_count == 1


async def test_may_khong_co_goi_hoac_tai_hong_thi_di_duong_rtsp(hass, aioclient_mock, tmp_path):
    url = "https://example.test/sdk.tar.gz"
    aioclient_mock.get(url, status=404)
    with mock.patch.object(hass.config, "path", side_effect=lambda *p: str(tmp_path.joinpath(*p))), \
            mock.patch.object(sdk_tai, "sdk_san_sang", return_value=False):
        with mock.patch.object(sdk_tai, "kien_truc", return_value="armv7l"):
            assert await sdk_tai.async_dam_bao_sdk(hass) is False
        with mock.patch.dict(sdk_tai.GOI, {"x86_64": (url, "0" * 64)}, clear=True), \
                mock.patch.object(sdk_tai, "kien_truc", return_value="x86_64"):
            assert await sdk_tai.async_dam_bao_sdk(hass) is False
    assert aioclient_mock.call_count == 1
