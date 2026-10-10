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


def test_co_huy_dung_giua_bai():
    """Stop / HA tắt: luồng phát thoát sau khúc đang gửi, bỏ phần còn trong hàng đợi."""
    phien = _Phien(0.0)
    loa = speaker.Speaker(None, lambda: phien)
    hang: queue.Queue = queue.Queue()
    for k in _song(5.0):                       # 5 giây tiếng đã nằm sẵn trong hàng đợi
        hang.put(k)
    hang.put(None)
    huy = threading.Event()
    threading.Timer(0.2, huy.set).start()
    t0 = time.monotonic()
    loa._phien(hang, TS, False, huy)
    assert time.monotonic() - t0 < 0.5
    assert sum(len(p) for _t, p in phien.gui) < 2 * TS * 0.5


async def test_bi_huy_thi_luong_phat_thoat_ngay(hass):
    """Đo 30/09/2026: HA mất 1 phút mới tắt được vì luồng phát YouTube ra loa camera còn gửi dở."""
    import asyncio

    dong = threading.Event()

    class _PhienDong(_Phien):
        def __exit__(self, *_a):
            dong.set()

    phien = _PhienDong(0.0)
    loa = speaker.Speaker(hass, lambda: phien)

    async def nguon():
        for k in _song(30.0):
            yield k

    viec = asyncio.ensure_future(loa.async_play_pcm(nguon(), TS))
    await asyncio.sleep(0.3)
    viec.cancel()
    try:
        await viec
    except asyncio.CancelledError:
        pass
    assert await hass.async_add_executor_job(dong.wait, 1.0), "luồng phát phải đóng kênh ngay khi bị huỷ"
    assert not loa.playing


def test_nhan_bien_do_giam_tang_va_chan_dinh():
    pcm = struct.pack("<3h", 1000, -1000, 30000)
    assert struct.unpack("<3h", speaker.nhan_bien_do(pcm, 0.5)) == (500, -500, 15000)
    assert struct.unpack("<3h", speaker.nhan_bien_do(pcm, 2.0)) == (2000, -2000, 32767), "chặn đỉnh, không tràn"
    assert speaker.nhan_bien_do(pcm, 1.0) is pcm


def test_am_luong_ap_cho_nhac_khong_ap_cho_bo_dam():
    for song, mong in ((False, 500), (True, 1000)):
        phien = _Phien(0.0)
        loa = speaker.Speaker(None, lambda phien=phien: phien)
        loa.he_so = 0.5
        hang: queue.Queue = queue.Queue()
        k = struct.pack(f"<{int(TS * 0.04)}h", *([1000] * int(TS * 0.04)))
        hang.put((time.monotonic(), k) if song else k)
        hang.put(None)
        loa._phien(hang, TS, song)
        assert struct.unpack_from("<h", phien.gui[0][1])[0] == mong


def test_khuc_tre_qua_lau_bi_bo_ca_khi_co_tieng(monkeypatch):
    """Mạng nghẽn rồi xả một loạt: tiếng nằm trong hàng quá ``TRE_TOI_DA_GIAY`` thì bỏ, khỏi kéo trễ cả cuộc gọi.
    Lúc chờ mở kênh (đăng nhập HCNetSDK, camera bận) không tính: câu đầu xếp hàng trước khi kênh mở vẫn phát."""
    monkeypatch.setattr(speaker, "TRE_TOI_DA_GIAY", 0.3)
    phien = _Phien(0.5)                            # mở kênh lâu hơn ngưỡng
    loa = speaker.Speaker(None, lambda: phien)
    hang: queue.Queue = queue.Queue()
    t0 = time.monotonic()
    cau1, cau2 = _song(0.2), _song(0.6, CAU_2)     # cả hai xếp hàng trước khi kênh mở
    for k in cau1 + cau2:
        hang.put((t0, k))
    hang.put(None)
    loa._phien(hang, TS, song=True)
    gui = b"".join(p for _t, p in phien.gui)
    assert gui.startswith(b"".join(cau1))          # câu đầu không mất vì chờ mở kênh
    assert len(gui) < 2 * TS * 0.5                 # đuôi câu 2 nằm hàng > 0,3 s sau khi mở: bỏ
