"""Loa camera: nguồn sống (bộ đàm) không được tích trễ.

Đo 29/09/2026 trên H6C: chờ mở kênh tới 1,25 s (camera vừa đóng kênh chưa nhả) + 0,3 s đệm đầu
câu; phát hết hàng đợi đúng nhịp thì cả lượt nói trễ theo — chủ máy: "bị trễ so với thực tế".
"""

import math
import queue
import struct
import threading
import time

from custom_components.dahua_talk import speaker

TS = 16000
KHUC = 0.02                                    # go2rtc / Opus: khúc 20 ms
CAU_2 = 2000                                   # biên độ câu 2 (câu 1: 8000)


def _song(giay: float, bien: int = 8000) -> list[bytes]:
    n = int(TS * KHUC)
    return [struct.pack(f"<{n}h", *(int(bien * math.sin(2 * math.pi * 300 * (k * n + i) / TS))
                                    for i in range(n))) for k in range(int(giay / KHUC))]


def _im(giay: float) -> list[bytes]:
    return [b"\x00\x00" * int(TS * KHUC)] * int(giay / KHUC)


class _Phien:
    """Phiên giả: mở kênh mất ``cho_mo`` giây, ``send_pcm`` phát đúng nhịp thời gian thực."""

    def __init__(self, cho_mo: float) -> None:
        self.cho_mo, self.tan_so, self.gui = cho_mo, TS, []

    def __enter__(self):
        time.sleep(self.cho_mo)
        return self

    def __exit__(self, *_a):
        pass

    def send_pcm(self, pcm: bytes) -> None:
        self.gui.append((time.monotonic(), pcm))
        time.sleep(len(pcm) / (2 * TS))


def _chay(song: bool) -> tuple[_Phien, float]:
    """Đệm 0,3 s im trước câu; câu 1 (0,5 s) → lặng 1 s → câu 2 (0,5 s); kênh mở chậm 1 s.
    Trả phiên và độ trễ lúc tiếng câu 2 bắt đầu ra loa."""
    phien = _Phien(1.0)
    loa = speaker.Speaker(None, lambda: phien)
    hang: queue.Queue = queue.Queue()
    t0 = time.monotonic()
    for i, k in enumerate(_im(0.3)):          # đệm đầu câu: tới trước lúc mở phiên
        hang.put((t0 - 0.3 + i * KHUC, k) if song else k)
    cau2 = []

    def day():
        for i, k in enumerate(_song(0.5) + _im(1.0) + _song(0.5, CAU_2)):
            luc = t0 + i * KHUC
            time.sleep(max(0.0, luc - time.monotonic()))
            if i == round(1.5 / KHUC):
                cau2.append(luc)
            hang.put((luc, k) if song else k)
        hang.put(None)

    threading.Thread(target=day, daemon=True).start()
    loa._phien(hang, TS, song)
    # Câu 2 nhỏ hơn câu 1 — nhận ra theo nội dung, không theo giờ (câu 1 có thể còn đang phát trễ).
    ra_loa = next(t for t, p in phien.gui if speaker.muc_db(p) > speaker.NGUONG_IM_DB
                  and max(abs(x) for (x,) in struct.iter_unpack("<h", p)) <= CAU_2)
    return phien, ra_loa - cau2[0]


def _mau_tieng(phien: _Phien) -> int:
    return sum(1 for _t, p in phien.gui for (x,) in struct.iter_unpack("<h", p) if x)


def test_nguon_song_duoi_kip_qua_khoang_lang():
    phien, tre = _chay(song=True)
    assert tre < 0.25                          # câu 2 ra loa gần như tức thì
    # Không bỏ mẫu tiếng nói nào (sin 300 Hz: vài mẫu đúng 0 — so với nguồn, không so tuyệt đối).
    goc = sum(1 for k in _song(0.5) + _song(0.5, CAU_2) for (x,) in struct.iter_unpack("<h", k) if x)
    assert _mau_tieng(phien) == goc


def test_khong_phai_nguon_song_thi_phat_du_nhu_cu():
    phien, tre = _chay(song=False)
    assert tre > 1.0                           # TTS / tệp: phát trọn, không bỏ gì
    assert sum(len(p) for _t, p in phien.gui) == 2 * int(TS * (0.3 + 0.5 + 1.0 + 0.5))
