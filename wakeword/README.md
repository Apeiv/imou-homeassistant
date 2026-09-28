# Từ gọi tiếng Việt «Trợ lý» cho openWakeWord

Mô hình từ gọi **«Trợ lý»** (và «Trợ lý ơi») cho
[wyoming-openwakeword](https://github.com/rhasspy/wyoming-openwakeword) 2.x, dùng với vệ tinh
Assist của tích hợp này hoặc bất kỳ vệ tinh nào trong Home Assistant.

Các mẫu có sẵn như «Okay Nabu» học từ giọng tiếng Anh — người Việt đọc không giống nên hay
trượt, nhất là qua mic camera ở xa.

## Chọn bản (bản 2 — 28/09/2026)

| Tệp | Độ nhạy | Gọi thật | Câu nói thường thức nhầm | Câu gần âm thức nhầm |
|---|---|---|---|---|
| `tro_ly_v2.tflite` | **vừa — nên dùng** | 170/171 giọng máy chưa học, 4/4 người thật | 1/114 (0,9 %) | 13–30 % |
| `tro_ly_v2_nhay.tflite` | cao — gọi xa / nói nhỏ | 171/171, 4/4 | 5/114 (4,4 %) | 16–31 % |
| `tro_ly_v2_it_nhay.tflite` | thấp — nhà ồn, hay nói to | 155/171, 3/4 | 0/114 | 9–15 % |

Cả ba dùng **ngưỡng mặc định 0,5** của container; độ nhạy nằm sẵn trong tệp. openWakeWord chỉ
có `--threshold` CHUNG cho mọi từ gọi, nên muốn mỗi loa / camera một độ nhạy thì dùng mỗi nơi một
tệp: pipeline nào chọn `tro_ly_v2_it_nhay` thì khó gọi hơn, pipeline khác không đổi.

**Bản cũ `tro_ly.tflite` — đừng dùng.** Đo lại theo LUỒNG (như container nghe mic liên tục), câu nói
thường thức nhầm 11 % («Tối nay ăn gì đây», «Mấy giờ rồi nhỉ»… điểm 0,99+ — hạ độ nhạy không cứu
được). Nguyên nhân: lúc học, câu có từ gọi luôn dứt sát cuối cửa sổ 2 giây còn câu thường đặt
ngẫu nhiên, nên mô hình học "tiếng vừa dứt ở cuối cửa sổ = đang gọi" — mà nghe liên tục thì câu
nào lúc dứt cũng vậy. Bản 2 đặt cả câu thường dứt sát cuối (70 %). Phép đo cũ ("tiếng sinh hoạt
điểm cao nhất 0,01") chỉ có tiếng nền, không có người nói, nên không bắt được lỗi này.

## Cài

1. Chép tệp đã chọn vào thư mục mô hình riêng của container openWakeWord (thư mục gắn vào
   `--custom-model-dir`), ví dụ:
   ```bash
   cd <thư mục mô hình riêng>
   wget https://raw.githubusercontent.com/TriTue2011/imou-homeassistant/main/wakeword/tro_ly_v2.tflite
   ```
2. Khởi động lại container openWakeWord.
3. Home Assistant → **Cài đặt → Trợ lý giọng nói** → chọn pipeline → **Từ đánh thức** → `tro_ly_v2`.

## Số đo

Chấm bằng `pyopen_wakeword` (đúng thư viện wyoming-openwakeword 2.x) theo LUỒNG: nối các câu
thành một dòng tiếng liên tục, cách nhau 1,5 giây tiếng nền, lấy điểm cao nhất của từng câu.
Giọng máy "chưa học" là hai họ giọng giữ riêng, không có trong dữ liệu học; "người thật" là 4
tin thoại của nhà (có trong dữ liệu học). "Câu gần âm" là câu có từ nghe na ná từ gọi — phần
khó nhất, còn nhầm khá nhiều ở mọi bản.

Học từ ~100 giọng tiếng Việt tổng hợp (câu có từ gọi, câu gần âm, câu nói thường) cộng vài
câu người thật, mỗi câu làm méo nhiều bản (vang phòng, dải tần mic camera, to nhỏ, nhanh chậm).
Đặc trưng là bộ trích embedding có sẵn của openWakeWord; đầu phân loại nhỏ đầu vào `[1, 16, 96]`.
Ba bản chỉ khác hệ số chệch của lớp cuối: trừ logit(ngưỡng) để mốc 0,5 của container bằng
ngưỡng 0,7 / 0,5 / 0,9 của mô hình.

## Tự thử trên bản ghi của bạn

```bash
pip install pyopen-wakeword numpy
python cham_tu_goi.py tro_ly.tflite ban_ghi.wav      # WAV 16 kHz mono, hoặc PCM16 thô .pcm
```

In điểm cao nhất và các mốc (giây) vượt 0,95. Tệp ngắn (một câu gọi) được đệm nền trước/sau.
