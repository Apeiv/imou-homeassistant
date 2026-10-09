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
from types import SimpleNamespace
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
mo, n_khung, so_mo = False, 0, 0
while len(d := sys.stdin.buffer.read(4)) == 4:
    n = struct.unpack(">I", d)[0]
    if n == 0xFFFFFFFF:
        so_mo += 1
        if so_mo >= int(os.environ.get("HIK_BAN_TU_LUOT", 10**9)):   # phiên SDK cũ hỏng: từ chối mãi
            bao.write("LOI camera không mở kênh đàm thoại (mã 29)\\n"); continue
        if ban := int(os.environ.get("HIK_BAN_CON", "0")):
            os.environ["HIK_BAN_CON"] = str(ban - 1)
            bao.write("LOI camera không mở kênh đàm thoại (mã 29)\\n"); continue
        mo, n_khung = True, 0; bao.write("OK\\n")
        if fd := os.environ.get("HIK_NGHE_FD"):                 # camera nói lại: 3 khung mic về
            os.write(int(fd), {ADTS!r} * 3)
    elif n == 0:
        if mo: nhat_ky.write(f"luot {{n_khung}}\\n")
        mo = False; bao.write("DONG\\n")
    else:
        k = sys.stdin.buffer.read(n)
        assert k == {ADTS!r}
        if not mo:
            continue                                     # khung lạc khi kênh đã đóng: bỏ (như hik_noi.c)
        n_khung += 1
        if n_khung == int(os.environ.get("HIK_HONG_SAU", "0")):
            bao.write("LOI gửi tiếng hỏng (mã 41)\\n"); mo = False
nhat_ky.write("dang_xuat\\n")
""")
    ffmpeg = _tep(tmp_path / "ffmpeg", f"""
import sys
if sys.argv[sys.argv.index("-f") + 1] != "s16le":           # giải mã (nghe): mỗi khung ADTS → 320 byte PCM
    while d := sys.stdin.buffer.read({len(ADTS)}):
        sys.stdout.buffer.write(b"\\x01\\x00" * 160); sys.stdout.buffer.flush()
    sys.exit()
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


def test_phien_cu_tu_choi_thi_dang_nhap_lai_mot_lan(gia, monkeypatch):
    """Tiến trình sống nhưng phiên SDK cũ từ chối mở kênh: đăng nhập lại một lần — trừ «mã 29» ngay sau lượt vừa
    nói (camera bận): báo lỗi luôn, không bỏ phiên đang ấm."""
    ffmpeg, ghi = gia
    monkeypatch.setenv("HIK_BAN_TU_LUOT", "2")              # bản đầu: lượt 1 mở được, lượt 2 từ chối mãi
    monkeypatch.setattr(hik_talk, "_CHO_NHA_KENH", 0.3)
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    with pytest.raises(TalkError, match="29"):               # vừa nói xong: bận
        mo().__enter__()
    mo._tg.ranh_tu -= 60                                     # lâu rồi không nói: phiên cũ hỏng
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    mo.close()
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 2", "dang_xuat", "dang_nhap", "luot 2", "dang_xuat"]


def test_dang_nhap_truoc_thi_luot_dau_khong_cho(gia):
    """Đăng nhập sẵn: lượt đầu không đăng nhập lại; mật khẩu sai chỉ ghi log và lượt nói báo lỗi."""
    ffmpeg, ghi = gia
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    mo.dang_nhap_truoc()
    assert _nhat_ky(ghi) == ["dang_nhap"]
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    mo.close()
    assert _nhat_ky(ghi) == ["dang_nhap", "luot 2", "dang_xuat"]
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "sai", sdk_dir="/x", ffmpeg=ffmpeg)
    mo.dang_nhap_truoc()
    assert mo._tg is None
    with pytest.raises(AuthError):
        mo().__enter__()
    mo.close()


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


@pytest.mark.parametrize("cong", [None, 8443])
async def test_them_ezviz_co_sdk_thi_noi_qua_hik(hass, cong):
    """Default HCNetSDK port 8000; 8443 (SDK over TLS, EZVIZ DB1C) when the user picks it."""
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "homeassistant", {})
    with mock.patch("custom_components.dahua_talk.sdk_tai.sdk_san_sang", return_value=True), \
            mock.patch("custom_components.dahua_talk.config_flow.check_hik_talk",
                       return_value="AAC") as kiem, \
            mock.patch("custom_components.dahua_talk.async_setup_entry", return_value=True):
        kq = await hass.config_entries.flow.async_init("dahua_talk", context={"source": "user"})
        kq = await hass.config_entries.flow.async_configure(kq["flow_id"], {"next_step_id": "ezviz"})
        kq = await hass.config_entries.flow.async_configure(kq["flow_id"], {
            "name": "Cam H6C", "host": "172.16.10.37", "password": "ABCDEF",
            **({"hik_port": cong} if cong else {})})
    assert kq["type"] is FlowResultType.CREATE_ENTRY
    assert kq["data"]["talk_protocol"] == "hik"
    assert kq["data"]["hik_port"] == (cong or hik_talk.CONG_HIK)
    assert kiem.call_args.args[:3] == ("172.16.10.37", "admin", "ABCDEF")
    assert kiem.call_args.args[4] == (cong or hik_talk.CONG_HIK)


async def test_cau_hinh_lai_ezviz_hik_doi_cong_thi_dang_nhap_lai(hass):
    """Reconfigure: same port keeps the entry without a login; 8000 -> 8443 logs in again on 8443."""
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain="dahua_talk", title="DB1C", unique_id="10.0.0.9:554", data={
        "name": "DB1C", "host": "10.0.0.9", "camera_type": "ezviz", "talk_protocol": "hik",
        "port": 554, "username": "admin", "password": "ABCDEF", "intercom_key": "k"})
    entry.add_to_hass(hass)
    with mock.patch("custom_components.dahua_talk.sdk_tai.sdk_san_sang", return_value=True),             mock.patch("custom_components.dahua_talk.config_flow.check_hik_talk",
                       return_value="AAC") as kiem,             mock.patch("custom_components.dahua_talk.async_setup_entry", return_value=True):
        for cong, goi in ((hik_talk.CONG_HIK, 0), (8443, 1)):
            kq = await entry.start_reconfigure_flow(hass)
            kq = await hass.config_entries.flow.async_configure(kq["flow_id"], {
                "host": "10.0.0.9", "hik_port": cong})
            assert kq["reason"] == "reconfigure_successful" and kiem.call_count == goi
    assert kiem.call_args.args[4] == 8443 and entry.data["hik_port"] == 8443


def test_mo_phien_noi_chon_hik():
    from custom_components.dahua_talk import _mo_phien_noi
    mo = _mo_phien_noi({"host": "h", "port": 554, "username": "admin", "password": "p",
                        "talk_protocol": "hik"}, "ffmpeg", "/config/hcnetsdk/lib")
    assert isinstance(mo, hik_talk.MoPhienHik) and mo.sdk_dir == "/config/hcnetsdk/lib"
    assert mo.port == 8000                           # entries from before hik_port keep 8000
    mo = _mo_phien_noi({"host": "h", "port": 554, "username": "admin", "password": "p",
                        "talk_protocol": "hik", "hik_port": 8443}, "ffmpeg", "/x")
    assert mo.port == 8443


def test_issue2_camera_rot_mang_giua_bai_thi_bao_loi_khong_gui_tiep(gia, monkeypatch):
    """Issue #2 (30/09/2026): EZVIZ rớt mạng giữa bài mà loa «đang phát» thêm 44 phút — hik_noi bỏ qua kết quả gửi.
    Nay chương trình trợ giúp báo «LOI …» giữa lượt và ``send_pcm`` ném ``TalkError`` ngay."""
    ffmpeg, _ghi = gia
    monkeypatch.setenv("HIK_HONG_SAU", "3")
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    t0 = time.monotonic()
    with pytest.raises(TalkError, match="gửi tiếng hỏng"):
        with mo() as s:
            for _ in range(30):                           # 30 giây tiếng nếu không ai chặn
                s.send_pcm(b"\x00\x01" * 16000)
    assert time.monotonic() - t0 < 8
    mo.close()


def test_hai_chieu_tieng_mic_camera_ve_qua_nghe(gia):
    """Đàm thoại hai chiều: khung SDK đưa về (HIK_NGHE_FD) → ffmpeg giải mã → ``nghe`` nhận PCM; không ``nghe`` thì
    không mở fd, không ffmpeg giải mã."""
    ffmpeg, _ghi = gia
    ve = []
    nghe = SimpleNamespace(feed=ve.append, nguoi_nghe=set())      # như intercom.Nghe
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg, nghe=nghe)
    assert not mo.hai_chieu                               # chưa ai nghe: VOX như cũ
    nghe.nguoi_nghe.add(1)
    assert mo.hai_chieu
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    het = time.monotonic() + 3
    while sum(map(len, ve)) < 3 * 320 and time.monotonic() < het:
        time.sleep(0.05)
    assert b"".join(ve) == b"\x01\x00" * 160 * 3
    ff = mo._tg._ff_nghe
    mo.close()
    assert ff.wait(3) is not None                         # bộ giải mã thoát cùng chương trình trợ giúp
    mo = hik_talk.MoPhienHik("10.0.0.9", "admin", "MA", sdk_dir="/x", ffmpeg=ffmpeg)
    assert not mo.hai_chieu
    with mo() as s:
        s.send_pcm(b"\x00\x01" * 2048)
    assert mo._tg._ff_nghe is None
    mo.close()
