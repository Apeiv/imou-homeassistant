# EZVIZ / Hikvision / camera ONVIF có loa

Cùng các thực thể như camera Imou (loa, vệ tinh Assist, bộ đàm), chạy trong mạng nhà, không
qua đám mây EZVIZ. Phần chung (Assist, bộ đàm, 4G, bảo mật) ở [README chính](README.md).

Tích hợp nói với camera theo một trong hai đường:

| Đường | Khi nào |
|---|---|
| **HCNetSDK, cổng 8000** (AAC 16 kHz) | Máy HA **x86_64** — tích hợp **tự tải SDK** khi thêm camera EZVIZ — **chắc chạy nhất** (vd EZVIZ H6C: không ONVIF, không kênh ngược RTSP) |
| **Kênh tiếng ngược RTSP/ONVIF, cổng 554** | Máy chưa có SDK (vd Raspberry Pi). Nhiều đời EZVIZ không có kênh ngược — HA báo `DESCRIBE failed (551)` — hoặc có mà loa câm ([issue #1](https://github.com/TriTue2011/imou-homeassistant/issues/1)) |

## HCNetSDK

Thêm camera EZVIZ (hoặc ⋮ → **Cấu hình lại** camera đã có) là tích hợp tự tải SDK Hikvision
(~10 MB, từ [TriTue2011/hcnetsdk-linux](https://github.com/TriTue2011/hcnetsdk-linux)), kiểm mã
sha256 rồi giải vào `/config/hcnetsdk/lib`. Tải hỏng thì dùng kênh ngược RTSP và ghi cảnh báo vào
nhật ký.

Tự chép tay (máy không ra được Internet): tải [Device Network SDK (Linux 64-bit)](https://www.hikvision.com/en/support/tools/hitools/clf4633a00e385d6ea/)
của Hikvision, chép thư mục **`lib`** (kể cả `HCNetSDKCom`) vào **`/config/hcnetsdk/lib`** — HA OS
dùng add-on Samba / File editor / SSH — rồi ⋮ → **Cấu hình lại** → lưu.

Camera phải mở cổng **8000** trong mạng nhà (EZVIZ Studio → Network → *Device Port*).

## Chuẩn bị camera

- **IP tĩnh**; mã hoá hình **H.264**; một số đời phải bật **RTSP / xem qua LAN** trong app.
- Tài khoản `admin` + **mã xác minh** (6 chữ IN HOA trên tem dưới đáy camera).
- Luồng chính `/Streaming/Channels/101`, luồng phụ `/Streaming/Channels/102`.

## Thêm camera

Thêm tích hợp → **Assist Camera** → loại **EZVIZ**: điền Tên, IP, **mã xác minh**; tick **Nghe
mic camera** nếu dùng làm vệ tinh Assist (URL mic tự dựng). Tích hợp đăng nhập thử, không phát
gì. Hikvision / ONVIF khác thì chọn loại **Hikvision / camera ONVIF khác**.

Mic EZVIZ nhỏ (phòng yên ~ −70 dBFS): đặt `number.<camera>_microphone_gain` khoảng **+18 dB**.

## go2rtc và bộ đàm

```yaml
streams:
  cam_ezviz:
    - rtsp://admin:MA_XAC_MINH@192.168.1.64:554/Streaming/Channels/101#backchannel=0
    - ffmpeg:cam_ezviz#audio=opus
    - "rtsp://127.0.0.1:8557/<mã>/<khoá>#backchannel=1"   # từ dahua_talk.get_intercom_source
  cam_ezviz_sub:
    - rtsp://admin:MA_XAC_MINH@192.168.1.64:554/Streaming/Channels/102
```

- `#backchannel=0` ở luồng camera: để bộ đàm đi qua tích hợp (dùng chung phiên với TTS/Assist),
  không để go2rtc tự mở kênh ngược của camera — hai bên cùng mở thì camera chỉ nhận một.
- Mật khẩu có `@` thì viết `%40`.
- Lấy dòng bộ đàm, `ha_url`, thẻ WebRTC: [README chính → Bộ đàm](README.md#bộ-đàm).
- **HCNetSDK:** nghỉ giữa câu 1,5–3,5 giây thì câu sau trễ ~1,2 giây — camera cần chừng đó mới
  mở lại kênh vừa đóng, và mic camera câm suốt lúc kênh mở nên không giữ kênh mở được.

## Sự cố thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| Thêm camera báo "không có kênh tiếng ngược", loa báo `DESCRIBE failed (551)` | Camera không có kênh ngược: cần HCNetSDK (máy x86_64 — ⋮ → Cấu hình lại để tải); hoặc sai đường dẫn luồng |
| Báo sai đăng nhập dù đúng mã | Camera đang khoá — chờ vài phút, thử **một** lần |
| Nói không ra loa, go2rtc báo lỗi kênh ngược | Thiếu `#backchannel=0` ở URL RTSP của camera |
| Không nối được cổng 554 / 8000 | Bật RTSP / xem qua LAN trong app EZVIZ; kiểm Device Port 8000 |
| WebRTC không có hình | Đổi sang H.264 |
