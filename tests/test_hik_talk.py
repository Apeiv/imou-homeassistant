"""Loa EZVIZ / Hikvision qua HCNetSDK cổng 8000 — chương trình trợ giúp và ffmpeg giả lập.

Đo thật 28/09/2026 trên EZVIZ H6C (không có kênh ngược RTSP, không ONVIF, HTTP bị khoá):
chương trình trợ giúp chạy qua trình nạp glibc ngay trong container HA Alpine, camera báo
AAC 16 kHz, loa phát — mic hai camera Imou gần đó bắt đỉnh 1 kHz cao hơn nền 15–19 dB đúng lúc
phát.
"""

import stat
import struct
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from custom_components.dahua_talk import hik_talk
from custom_components.dahua_talk.talk import AuthError, TalkError

ADTS = bytes([0xFF, 0xF1, 0x60, 0x40, 0x01, 0x5F, 0xFC]) + b"a" * 3      # một khung dài 10


def _tep(p: Path, noi_dung: str) -> str:
    p.write_text(f"#!{sys.executable}\n" + noi_dung)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.fixture
def gia(tmp_path):
    """Trợ giúp giả nói đúng giao thức (ghi số lần đăng nhập + khung từng lượt) + ffmpeg giả
    (mỗi 2048 byte PCM → một khung ADTS)."""
    ghi = tmp_path / "nhan.txt"
    tro_giup = _tep(tmp_path / "hik_noi", f"""
import os, struct, sys
bao = os.fdopen(int(os.environ["HIK_BAO_FD"]), "w", buffering=1)
if os.environ.get("HIK_MK") == "sai":
    bao.write("LOI đăng nhập cổng 8000 không được (mã 1)\\n"); sys.exit(3)
nhat_ky = open({str(ghi)!r}, "a", buffering=1)
nhat_ky.write("dang_nhap\\n")
bao.write("SAN AAC 16000\\n")
mo, n_khung = False, 0
while len(d := sys.stdin.buffer.read(4)) == 4:
    n = struct.unpack(">I", d)[0]
    if n == 0xFFFFFFFF:
        if ban := int(os.environ.get("HIK_BAN_CON", "0")):
            os.environ["HIK_BAN_CON"] = str(ban - 1)
            bao.write("LOI camera không mở kênh đàm thoại (mã 29)\\n"); continue
        mo, n_khung = True, 0; bao.write("OK\\n")
    elif n == 0:
        if mo: nhat_ky.write(f"luot {{n_khung}}\\n")
        mo = False; bao.write("DONG\\n")
    else:
        k = sys.stdin.buffer.read(n)
        assert k == {ADTS!r}
        n_khung += 1
nhat_ky.write("dang_xuat\\n")
""")
    ffmpeg = _tep(tmp_path / "ffmpeg", f"""
import sys
while d := sys.stdin.buffer.read(2048):
    sys.stdout.buffer.write({ADTS!r}); sys.stdout.buffer.flush()
""")
    with mock.patch.object(hik_talk, "_lenh", lambda *a: [tro_giup]):
        yield ffmpeg, ghi


def _nhat_ky(ghi: Path) -> list[str]:
    return ghi.read_text().split("\n")[:-1] if ghi.exists() else []


def test_luot_noi_gui_khung_adts_dung_giao_thuc_va_giu_nhip(gia):
    ffmpeg, ghi = gia
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    t0 = time.monotonic()
    with mo() as s:
        assert (s.ma, s.tan_so) == ("AAC", 16000)
        s.send_pcm(b"\x00\x01" * 16000)                   # 1 giây ở 16 kHz = 32 000 byte
    assert time.monotonic() - t0 >= 0.8                   # trả về gần lúc tiếng dứt
    # 32 000 byte / 2048 = 15 khung trọn + 1 khung phần dư cuối (ffmpeg giả nhả mọi lần đọc)
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 16"]
    mo.close()
    assert _nhat_ky(ghi)[-1] == "dang_xuat"


def test_giu_dang_nhap_giua_cac_luot_noi(gia):
    """Đo 29/09/2026: đăng nhập H6C 0,6–1,3 s, mở kênh 0,02–0,27 s — lượt sau không đăng nhập lại."""
    ffmpeg, ghi = gia
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    for _ in range(3):
        with mo() as s:
            s.send_pcm(b"\x00\x01" * 2048)
    mo.close()
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 2", "luot 2", "luot 2", "dang_xuat"]


def test_ban_tro_giup_chet_thi_dang_nhap_lai_mot_lan(gia):
    ffmpeg, ghi = gia
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    mo._tg.p.kill()                                       # camera khởi động lại / tiến trình chết
    mo._tg.p.wait()
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    mo.close()
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 2", "dang_nhap", "luot 2", "dang_xuat"]


def test_sap_toi_luc_tu_thoat_vi_ngoi_yen_thi_dung_ban_moi(gia):
    ffmpeg, ghi = gia
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    mo._tg.ranh_tu -= hik_talk.NGHI_GIAY                # đã ngồi yên quá lâu
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    mo.close()
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 2", "dang_xuat", "dang_nhap", "luot 2", "dang_xuat"]


def test_camera_chua_nha_kenh_thi_cho_roi_mo_lai(gia, monkeypatch):
    """Đo 29/09/2026: mở lại ngay sau lúc đóng thì H6C từ chối (mã 29); ~1,2 s sau là mở được."""
    ffmpeg, ghi = gia
    monkeypatch.setenv("HIK_BAN_CON", "2")
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    t0 = time.monotonic()
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    assert time.monotonic() - t0 >= 0.6                   # hai lần chờ 0,3 s
    mo.close()
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 2", "dang_xuat"]


def test_camera_ban_qua_lau_thi_bao_loi(gia, monkeypatch):
    ffmpeg, _ghi = gia
    monkeypatch.setenv("HIK_BAN_CON", "100")
    monkeypatch.setattr(hik_talk, "_CHO_NHA_KENH", 0.5)
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    with pytest.raises(TalkError, match="29"):
        mo().__enter__()
    mo.close()


def test_sai_mat_khau_bao_loi_dang_nhap(gia):
    ffmpeg, _ghi = gia
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "sai", sdk_dir="/x", ffmpeg=ffmpeg)
    with pytest.raises(AuthError):
        mo().__enter__()


def test_kiem_luc_them_camera_khong_gui_tieng(gia):
    _ffmpeg, ghi = gia
    assert hik_talk.check_hik_talk("10.0.0.9", "admin", "MA", "/x") == "AAC"
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 0", "dang_xuat"]


def test_mo_phien_nho_tan_so_camera_bao(gia):
    ffmpeg, _ghi = gia
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    mo.tan_so = 8000
    with mo() as s:
        assert s.tan_so == 16000
    assert mo.tan_so == 16000
    mo.close()


def test_khong_co_ban_tro_giup_cho_may_nay_thi_bao_ro():
    with mock.patch.object(hik_talk, "kien_truc", return_value="riscv64"):
        with pytest.raises(TalkError, match="riscv64"):
            hik_talk._lenh("10.0.0.9", 8000, "admin", "/x")


def test_sdk_san_sang_can_ca_sdk_lan_ban_tro_giup(tmp_path):
    assert not hik_talk.sdk_san_sang(str(tmp_path))
    (tmp_path / "libhcnetsdk.so").write_bytes(b"")
    assert hik_talk.sdk_san_sang(str(tmp_path)) == (
        hik_talk._THU_MUC / f"hik_noi-{hik_talk.kien_truc()}").is_file()


async def test_them_ezviz_co_sdk_thi_noi_qua_hik(hass):
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "homeassistant", {})
    with mock.patch("custom_components.dahua_talk.config_flow.sdk_san_sang", return_value=True), \
            mock.patch("custom_components.dahua_talk.config_flow.check_hik_talk",
                       return_value="AAC") as kiem, \
            mock.patch("custom_components.dahua_talk.async_setup_entry", return_value=True):
        kq = await hass.config_entries.flow.async_init("dahua_talk", context={"source": "user"})
        kq = await hass.config_entries.flow.async_configure(kq["flow_id"], {"next_step_id": "ezviz"})
        kq = await hass.config_entries.flow.async_configure(kq["flow_id"], {
            "name": "Cam H6C", "host": "172.16.10.37", "password": "ABCDEF"})
    assert kq["type"] is FlowResultType.CREATE_ENTRY
    assert kq["data"]["talk_protocol"] == "hik"
    assert kiem.call_args.args[:3] == ("172.16.10.37", "admin", "ABCDEF")


def test_mo_phien_noi_chon_hik():
    from custom_components.dahua_talk import _mo_phien_noi
    mo = _mo_phien_noi({"host": "h", "port": 554, "username": "admin", "password": "p",
                        "talk_protocol": "hik"}, "ffmpeg", "/config/hcnetsdk/lib")
    assert isinstance(mo, hik_talk.MoPhienHik) and mo.sdk_dir == "/config/hcnetsdk/lib"
