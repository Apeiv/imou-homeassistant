"""Bộ đàm trong HA: go2rtc POST A-law 8 kHz → loa camera, không cần dịch vụ ngoài."""

import asyncio
import bisect
import math
import struct
from types import SimpleNamespace
from unittest import mock

import aiohttp
import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.dahua_talk import intercom
from custom_components.dahua_talk.const import DOMAIN
from custom_components.dahua_talk.speaker import Speaker


def _song(giay: float, db: float) -> bytes:
    bien = 32767 * 10 ** (db / 20) * math.sqrt(2)
    n = int(giay * 8000)
    return struct.pack(f"<{n}h", *(int(bien * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(n)))


def _im(giay: float) -> bytes:
    return b"\x00" * (int(giay * 8000) * 2)


def _alaw(pcm: bytes) -> bytes:
    """Mã hoá bằng tra ngược bảng giải — đủ cho đo mức."""
    goc = sorted(range(256), key=lambda a: intercom._ALAW[a])
    gt = [intercom._ALAW[a] for a in goc]
    ra = bytearray()
    for (x,) in struct.iter_unpack("<h", pcm):
        i = min(bisect.bisect_left(gt, x), 255)
        if i and abs(gt[i - 1] - x) <= abs(gt[i] - x):
            i -= 1
        ra.append(goc[i])
    return bytes(ra)


class LoaGia:
    hai_chieu = False

    def __init__(self):
        self.phien: list[bytes] = []

    async def async_play_pcm(self, chunks, tan_so=8000, song=False):
        du = b""
        async for c in chunks:
            du += c[1] if song else c
        self.phien.append(du)
        self.tan_so = tan_so
        return len(du) / (2 * tan_so)


def _day(ic, pcm, khuc=320):
    for i in range(0, len(pcm), khuc):
        ic.feed(pcm[i:i + khuc])


def test_giai_alaw_dung_moc_itu():
    # 0xD5 / 0x55 là hai mã gần 0 nhất (+8 / -8) trong bảng G.711 A-law.
    assert struct.unpack("<2h", intercom.alaw_to_pcm(bytes([0xD5, 0x55]))) == (8, -8)


async def test_im_khong_mo_loa_co_tieng_thi_mo_im_thi_dong(hass):
    loa = LoaGia()
    ic = intercom.Intercom(hass, loa)
    _day(ic, _im(2) + _song(1.0, -60))           # mic tắt / phòng yên
    await asyncio.sleep(0)
    assert loa.phien == [] and not ic._viec
    _day(ic, _song(1.0, -20))                    # nói
    _day(ic, _im(intercom.IM_GIAY + 0.1))        # im đủ lâu → đóng để nghe bên kia
    _day(ic, _song(0.5, -20))                    # câu sau → phiên mới
    await ic.async_close()
    assert len(loa.phien) == 2
    # Giữ ~0,3 s trước tiếng: âm đầu câu không mất.
    assert len(loa.phien[0]) >= int((1.0 + intercom.DEM_GIAY - 0.05) * 16000)


def _muc():
    return MockConfigEntry(domain=DOMAIN, title="Cam khách", data={
        "name": "Cam khách", "host": "192.168.1.65", "port": 37777,
        "username": "admin", "password": "mk", "mic_url": ""})


def _hik():
    return MockConfigEntry(domain=DOMAIN, title="Cam hik", data={
        "name": "Cam hik", "host": "192.168.1.66", "port": 554, "username": "admin", "password": "mk",
        "mic_url": "", "talk_protocol": "hik", "hik_port": 8443})


def _mp_cua(hass, muc):
    return next(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), muc.entry_id)
                if e.domain == "media_player")


async def _cho_het_goi(muc):
    for _ in range(100):
        if muc.entry_id not in intercom._DANG_GOI:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("cuộc gọi không đóng")


async def _nap(hass, muc):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "http", {})
    muc.add_to_hass(hass)
    assert await hass.config_entries.async_setup(muc.entry_id)
    await hass.async_block_till_done()


async def test_khoa_sinh_mot_lan_va_giu_nguyen(hass):
    muc = _muc()
    await _nap(hass, muc)
    khoa = muc.data[intercom.CONF_INTERCOM_KEY]
    assert len(khoa) >= 24
    await hass.config_entries.async_reload(muc.entry_id)
    await hass.async_block_till_done()
    assert muc.data[intercom.CONF_INTERCOM_KEY] == khoa   # dòng đã dán vào go2rtc vẫn đúng


async def test_dich_vu_tra_dong_go2rtc(hass):
    muc = _muc()
    await _nap(hass, muc)
    mp = _mp_cua(hass, muc)
    kq = await hass.services.async_call(DOMAIN, "get_intercom_source", {"entity_id": mp},
                                        blocking=True, return_response=True)
    nguon = kq["source"]
    assert nguon.startswith("exec:ffmpeg ") and nguon.endswith("#backchannel=1#audio=alaw/8000")
    assert "-probesize 32 -analyzeduration 0 -fflags nobuffer" in nguon   # trực tiếp, không gom
    url = next(t for t in nguon.split("#")[0].split() if t.startswith("http"))
    assert f"/api/dahua_talk/intercom/{muc.entry_id}?k={muc.data[intercom.CONF_INTERCOM_KEY]}" in url
    assert url.startswith("http://127.0.0.1:")


async def test_post_khoa_sai_bi_chan_khoa_dung_thi_phat(hass, hass_client_no_auth):
    muc = _muc()
    await _nap(hass, muc)
    loa = LoaGia()
    client = await hass_client_no_auth()
    duong = f"/api/dahua_talk/intercom/{muc.entry_id}"
    tieng = _alaw(_song(1.0, -20))
    with mock.patch.object(Speaker, "async_play_pcm", loa.async_play_pcm):
        assert (await client.post(duong, data=tieng)).status == 401
        assert (await client.post(duong + "?k=sai", data=tieng)).status == 401
        assert (await client.post("/api/dahua_talk/intercom/khong-co?k=x", data=tieng)).status == 401
        assert loa.phien == []
        r = await client.post(f"{duong}?k={muc.data[intercom.CONF_INTERCOM_KEY]}", data=tieng)
        assert r.status == 200
        assert (await r.json())["seconds"] > 0.9
    assert len(loa.phien) == 1


async def test_ha_url_cho_go2rtc_o_may_khac(hass):
    """Proxmox / Frigate / container mạng bridge: go2rtc không gọi được 127.0.0.1 của HA."""
    muc = _muc()
    await _nap(hass, muc)
    mp = _mp_cua(hass, muc)
    kq = await hass.services.async_call(DOMAIN, "get_intercom_source",
                                        {"entity_id": mp, "ha_url": "http://192.168.1.10:8123/"},
                                        blocking=True, return_response=True)
    assert f" http://192.168.1.10:8123/api/dahua_talk/intercom/{muc.entry_id}?k=" in kq["source"]
    for sai in ("192.168.1.10:8123", "http://a b:8123", "http://ha#x:8123"):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "get_intercom_source",
                                           {"entity_id": mp, "ha_url": sai},
                                           blocking=True, return_response=True)


async def test_hai_chieu_mo_ngay_va_khong_dong_khi_im(hass):
    """HCNetSDK đưa tiếng camera về: kênh mở từ khúc đầu (dù im) và giữ suốt phiên — không VOX."""
    loa = LoaGia()
    loa.hai_chieu = True
    ic = intercom.Intercom(hass, loa)
    _day(ic, _im(intercom.IM_GIAY + 1) + _song(0.5, -20) + _im(intercom.IM_GIAY + 1))
    assert ic._hang is not None                      # vẫn mở sau quãng im dài
    await ic.async_close()
    assert len(loa.phien) == 1 and len(loa.phien[0]) == (2 * (intercom.IM_GIAY + 1) + 0.5) * 16000


async def test_nghe_phat_tieng_camera_cho_the(hass, hass_client):
    """Chỉ camera HCNetSDK có tiếng về: mục Dahua trả 404; thẻ ngắt lúc camera im → bỏ đăng ký ngay."""

    dahua = _muc()
    await _nap(hass, dahua)
    client = await hass_client()
    assert (await client.get(f"/api/dahua_talk/listen/{_mp_cua(hass, dahua)}")).status == 404
    assert (await client.get("/api/dahua_talk/listen/media_player.khong_co")).status == 404
    muc = _hik()
    muc.add_to_hass(hass)
    assert await hass.config_entries.async_setup(muc.entry_id)
    await hass.async_block_till_done()
    r = await client.get(f"/api/dahua_talk/listen/{_mp_cua(hass, muc)}")
    assert r.status == 200
    nghe = muc.runtime_data.mic_camera
    await asyncio.sleep(0)
    assert muc.runtime_data.speaker.hai_chieu            # có người nghe → bộ đàm giữ kênh mở
    nghe.feed(b"\x01\x02" * 160)                    # từ luồng ffmpeg
    await asyncio.sleep(0)
    assert await r.content.readexactly(320) == b"\x01\x02" * 160
    r.close()
    for _ in range(50):                             # thẻ ngắt lúc camera im → bỏ đăng ký, không chờ khúc sau
        if not nghe.nguoi_nghe:
            break
        await asyncio.sleep(0.02)
    assert not nghe.nguoi_nghe and not muc.runtime_data.speaker.hai_chieu


class LoaHong(LoaGia):
    """Loa hai chiều mà lượt đầu hỏng (camera từ chối kênh)."""
    hai_chieu = True

    def __init__(self):
        super().__init__()
        self.lan = 0

    async def async_play_pcm(self, chunks, tan_so=8000, song=False):
        self.lan += 1
        if self.lan == 1:
            raise RuntimeError("camera từ chối")
        return await super().async_play_pcm(chunks, tan_so, song)


async def test_loa_hong_thi_cho_cho_loi_roi_mo_lai(hass, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(intercom, "time", SimpleNamespace(monotonic=lambda: t[0]))   # chỉ đồng hồ của intercom
    loa = LoaHong()
    ic = intercom.Intercom(hass, loa)
    ic.feed(_song(0.1, -20))
    await asyncio.sleep(0.01)                        # lượt đầu hỏng → đóng kênh, nhớ lúc hỏng
    assert ic._hang is None and loa.lan == 1
    ic.feed(_im(0.1))                                # trong ``_CHO_LOI``: im thì không mở lại (VOX)
    assert ic._hang is None
    t[0] += intercom._CHO_LOI + 0.1
    ic.feed(_im(0.1))                                # hết chờ: hai chiều mở lại từ khúc đầu
    assert ic._hang is not None
    await ic.async_close()
    assert loa.lan == 2 and len(ic._viec) == 1       # việc đã xong không tích lại


async def test_noi_qua_lau_thi_dong_kenh(hass, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(intercom, "time", SimpleNamespace(monotonic=lambda: t[0]))   # chỉ đồng hồ của intercom
    loa = LoaGia()
    loa.hai_chieu = True
    ic = intercom.Intercom(hass, loa)
    ic.feed(_im(0.1))
    t[0] += intercom.TOI_DA_NOI_GIAY
    ic.feed(_im(0.1))
    assert ic._hang is None and ic.het_gio
    ic.feed(_song(0.1, -20))                         # cả tiếng to cũng không mở lại trong phiên này
    assert ic._hang is None
    await ic.async_close()
    assert len(loa.phien) == 1


async def test_nghe_dong_tha_ca_hang_day(hass):
    nghe = intercom.Nghe(hass)
    hang = asyncio.Queue()
    nghe.nguoi_nghe.add(hang)
    for _ in range(intercom.TOI_DA_KHUC_NGHE + 5):
        nghe._phat(b"\x00\x00")
    assert hang.qsize() == intercom.TOI_DA_KHUC_NGHE   # trần hàng đợi
    nghe.close()
    assert hang.qsize() == intercom.TOI_DA_KHUC_NGHE + 1
    for _ in range(intercom.TOI_DA_KHUC_NGHE):
        hang.get_nowait()
    assert hang.get_nowait() is None                  # hàng đầy vẫn nhận tín hiệu hết


async def test_listen_sau_go_muc_tra_404(hass, hass_client):
    muc = _hik()
    await _nap(hass, muc)
    mp = _mp_cua(hass, muc)
    client = await hass_client()
    r = await client.get(f"/api/dahua_talk/listen/{mp}")
    assert r.status == 200
    nghe = muc.runtime_data.mic_camera
    await asyncio.sleep(0)
    assert await hass.config_entries.async_unload(muc.entry_id)   # gỡ mục: người đang nghe được thả
    assert await r.content.read() == b""
    assert (await client.get(f"/api/dahua_talk/listen/{mp}")).status == 404


async def test_call_ws_chan_khong_token_va_muc_khong_phai_hik(hass, hass_client, hass_client_no_auth):
    dahua, hik = _muc(), _hik()
    await _nap(hass, dahua)
    hik.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hik.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(aiohttp.WSServerHandshakeError) as loi:
        await (await hass_client_no_auth()).ws_connect(f"/api/dahua_talk/call_ws/{_mp_cua(hass, hik)}")
    assert loi.value.status == 401
    client = await hass_client()
    for eid in (_mp_cua(hass, dahua), "media_player.khong_co"):
        with pytest.raises(aiohttp.WSServerHandshakeError) as loi:
            await client.ws_connect(f"/api/dahua_talk/call_ws/{eid}")
        assert loi.value.status == 404
    assert not hik.runtime_data.mic_camera.nguoi_nghe


async def test_call_ws_hai_chieu_va_chi_mo_kenh_khi_co_0x02(hass, hass_client):
    muc = _hik()
    await _nap(hass, muc)
    loa = LoaGia()
    goi = mock.AsyncMock(side_effect=loa.async_play_pcm)
    with mock.patch.object(Speaker, "async_play_pcm", goi):
        ws = await (await hass_client()).ws_connect(f"/api/dahua_talk/call_ws/{_mp_cua(hass, muc)}")
        assert await ws.receive_json() == {"type": "ready"}
        await ws.send_bytes(b"\x01" + _song(0.1, -20))   # byte đầu lạ, khung chữ: bỏ qua
        await ws.send_str("xin chào")
        await asyncio.sleep(0.1)
        assert not goi.called                           # chưa có 0x02: kênh nói HCNetSDK chưa mở
        tieng = _song(0.2, -20)
        await ws.send_bytes(b"\x02" + tieng)
        nghe = muc.runtime_data.mic_camera
        nghe.feed(b"\x05\x06" * 160)                    # tiếng camera về
        msg = await ws.receive()
        assert msg.data == b"\x01" + b"\x05\x06" * 160
        await ws.close()
        await _cho_het_goi(muc)
    assert goi.call_count == 1 and loa.phien == [tieng]
    assert not nghe.nguoi_nghe


async def test_call_ws_cuoc_thu_hai_bao_ban(hass, hass_client):
    muc = _hik()
    await _nap(hass, muc)
    client = await hass_client()
    duong = f"/api/dahua_talk/call_ws/{_mp_cua(hass, muc)}"
    ws1 = await client.ws_connect(duong)
    assert await ws1.receive_json() == {"type": "ready"}
    ws2 = await client.ws_connect(duong)
    assert (await ws2.receive()).type == aiohttp.WSMsgType.CLOSE and ws2.close_code == intercom.MA_BAN
    assert len(muc.runtime_data.mic_camera.nguoi_nghe) == 1
    await ws1.close()
    await _cho_het_goi(muc)
    ws3 = await client.ws_connect(duong)
    assert await ws3.receive_json() == {"type": "ready"}
    await ws3.close()
    await _cho_het_goi(muc)


async def test_call_ws_qua_tran_thi_cup_may(hass, hass_client, monkeypatch):
    monkeypatch.setattr(intercom, "TOI_DA_NOI_GIAY", 0.2)
    muc = _hik()
    await _nap(hass, muc)
    ws = await (await hass_client()).ws_connect(f"/api/dahua_talk/call_ws/{_mp_cua(hass, muc)}")
    assert await ws.receive_json() == {"type": "ready"}
    assert (await ws.receive()).type == aiohttp.WSMsgType.CLOSE and ws.close_code == intercom.MA_HET_GIO
    await _cho_het_goi(muc)
    assert not muc.runtime_data.mic_camera.nguoi_nghe


async def test_call_ws_go_muc_thi_dong(hass, hass_client):
    muc = _hik()
    await _nap(hass, muc)
    ws = await (await hass_client()).ws_connect(f"/api/dahua_talk/call_ws/{_mp_cua(hass, muc)}")
    assert await ws.receive_json() == {"type": "ready"}
    assert await hass.config_entries.async_unload(muc.entry_id)
    assert (await ws.receive()).type == aiohttp.WSMsgType.CLOSE
    await _cho_het_goi(muc)


async def test_call_ws_loa_dang_phat_thi_ban(hass, hass_client):
    """TTS / thông báo / bộ đàm go2rtc đang giữ loa: cuộc gọi không chen vào."""
    muc = _hik()
    await _nap(hass, muc)
    muc.runtime_data.speaker.playing = True
    ws = await (await hass_client()).ws_connect(f"/api/dahua_talk/call_ws/{_mp_cua(hass, muc)}")
    assert (await ws.receive()).type == aiohttp.WSMsgType.CLOSE and ws.close_code == intercom.MA_BAN
    assert not muc.runtime_data.mic_camera.nguoi_nghe and muc.entry_id not in intercom._DANG_GOI


async def test_call_ws_loi_van_tha_ban(hass, hass_client):
    muc = _hik()
    await _nap(hass, muc)
    with mock.patch.object(intercom.CallView, "_goi", side_effect=RuntimeError("hỏng")):
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await (await hass_client()).ws_connect(f"/api/dahua_talk/call_ws/{_mp_cua(hass, muc)}")
    assert muc.entry_id not in intercom._DANG_GOI
