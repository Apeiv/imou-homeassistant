"""Loa camera Imou qua cổng 8086 (AAC 16 kHz) — camera giả, và đổi tần số ở loa.

Đo thật 28/09/2026 trên bốn camera Imou: bắt tay 200 OK trong 0,03 s; nghe so sánh cùng câu,
16 kHz qua 8086 rõ hơn 8 kHz qua 37777.
"""

import math
import shutil
import socket
import struct
import threading
import time
from unittest import mock

import pytest

from custom_components.dahua_talk import http_talk
from custom_components.dahua_talk.speaker import doi_tan_so
from custom_components.dahua_talk.talk import TalkError


# Camera giả nghe trên 127.0.0.1 — bộ test HA chặn socket nên mở riêng cho tệp này.
@pytest.fixture(autouse=True)
def _mo_socket(socket_enabled):
    yield


class Camera8086Gia:
    """Máy chủ 8086 giả: 200 cho mọi PLAY (hay 401 kèm realm lần đầu), gom khung tiếng."""

    def __init__(self, doi_realm: bool = False) -> None:
        self.doi_realm = doi_realm
        self.ln = socket.create_server(("127.0.0.1", 0))
        self.cong = self.ln.getsockname()[1]
        self.yeu_cau: list[str] = []
        self.khung: list[bytes] = []
        self.luong = threading.Thread(target=self._nghe, daemon=True)
        self.luong.start()

    def _nghe(self):
        c, _ = self.ln.accept()
        du = b""
        try:
            while b := c.recv(65536):
                du += b
                while True:
                    if du.startswith(b"$") and len(du) >= 6:
                        dai = 6 + struct.unpack_from(">I", du, 2)[0]
                        if len(du) < dai:
                            break
                        self.khung.append(du[:dai])
                        du = du[dai:]
                    elif b"\r\n\r\n" in du:
                        dau, _, du = du.partition(b"\r\n\r\n")
                        chu = dau.decode()
                        m = [x for x in chu.split("\r\n") if x.startswith("Private-Length: ")]
                        du = du[int(m[0].split(": ")[1]):] if m else du
                        self.yeu_cau.append(chu)
                        if self.doi_realm and len(self.yeu_cau) == 1:
                            c.sendall(b'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Digest '
                                      b'realm="Login to X", nonce="1"\r\nContent-Length: 0\r\n\r\n')
                        else:
                            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    else:
                        break
        except OSError:
            pass


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="cần ffmpeg")
def test_phien_8086_bat_tay_va_gui_aac_16k_dung_nhip():
    cam = Camera8086Gia()
    t0 = time.monotonic()
    with http_talk.HttpTalkSession("127.0.0.1", "admin", "mk", port=cam.cong) as s:
        assert s.tan_so == 16000
        s.send_pcm(b"\x10\x00" * 16000)                            # 1 giây
        tra_ve = time.monotonic() - t0
    cam.luong.join(2)                      # bộ test HA đòi không còn luồng nào sót lại
    assert tra_ve >= 0.8                   # send_pcm trả về gần lúc tiếng dứt (mốc chặn mic ting)
    assert [y.split("trackID=")[1].split("&")[0] for y in cam.yeu_cau] == ["31", "6", "64"]
    assert "talktype=talk" in cam.yeu_cau[2] and 'Username="admin"' in cam.yeu_cau[0]
    assert 14 <= len(cam.khung) <= 20                              # ~1 giây / 64 ms mỗi khung
    k0 = cam.khung[0]
    assert k0[:2] == b"$\x0a" and k0[6:10] == b"DHAV" and k0[0x1E:0x22] == b"\x83\x01\x1a\x04"
    assert k0[6 + 0x17] == sum(k0[6:6 + 0x17]) & 0xFF
    assert k0[6 + 28] == 0xFF and k0[-8:-4] == b"dhav"


def test_camera_doi_realm_thi_bam_lai_mot_lan():
    cam = Camera8086Gia(doi_realm=True)
    with mock.patch.object(http_talk.subprocess, "Popen"), \
            mock.patch.object(http_talk.threading, "Thread"):
        k = http_talk.HttpTalkSession("127.0.0.1", "admin", "mk", port=cam.cong)
        k._mo()
    k.s.close()
    cam.luong.join(2)
    assert len(cam.yeu_cau) == 4                                    # 31 (401) → 31 → 6 → 64
    so = [x.split('PasswordDigest="')[1].split('"')[0] for x in cam.yeu_cau[:2]]
    assert so[0] != so[1]


def test_camera_khong_mo_8086_thi_lui_37777_va_nho():
    mo = http_talk.MoPhienImou("10.0.0.9", "u", "p", port=37777)
    goi = []

    class P37777:
        tan_so = 8000

        def __init__(self, *a, **k):
            goi.append(k.get("port"))

        def __enter__(self):
            return self

        def close(self):
            pass

    with mock.patch.object(http_talk.HttpTalkSession, "_mo",
                           side_effect=ConnectionRefusedError("đóng")) as thu, \
            mock.patch.object(http_talk, "TalkSession", P37777):
        assert mo.tan_so == 16000
        with mo() as s:
            assert s.tan_so == 8000
        assert mo.tan_so == 8000                                    # lần sau sinh tiếng 8 kHz luôn
        with mo():
            pass
    assert thu.call_count == 1 and goi == [37777, 37777]


def test_camera_tu_choi_8086_bang_ma_loi_cung_lui():
    cam = Camera8086Gia()
    cam_that = cam.cong

    def tu_choi(self, track, **k):
        return 403

    with mock.patch.object(http_talk.HttpTalkSession, "_play", tu_choi):
        with pytest.raises(TalkError, match="8086"):
            http_talk.HttpTalkSession("127.0.0.1", "u", "p", port=cam_that).__enter__()


def test_doi_tan_so_8k_16k():
    n = 800
    pcm8 = struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(n)))
    pcm16 = doi_tan_so(pcm8, 8000, 16000)
    assert len(pcm16) == 2 * len(pcm8)
    lai = doi_tan_so(pcm16, 16000, 8000)
    a, b = struct.unpack(f"<{n}h", pcm8), struct.unpack(f"<{n}h", lai)
    assert max(abs(x - y) for x, y in zip(a, b)) <= 2               # đi rồi về gần như nguyên vẹn
    assert doi_tan_so(pcm8, 8000, 8000) is pcm8


def test_cat_adts_giu_phan_do():
    k = bytes([0xFF, 0xF1, 0x50, 0x80, 0x01, 0x5F, 0xFC]) + b"x" * 3   # khung dài 10
    ra, con = http_talk.cat_adts(k + k + k[:4])
    assert ra == [k, k] and con == k[:4]
