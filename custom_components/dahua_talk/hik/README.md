# hik — nói qua HCNetSDK (Hikvision / EZVIZ, cổng 8000)

- `hik_noi.c` — mã nguồn chương trình trợ giúp. Dựng lại: `gcc -O2 -o hik_noi-x86_64 hik_noi.c -ldl`
  (đã dựng trên Ubuntu 22.04, glibc 2.35; dựng lại 01/10/2026 cho issue #2 — cần glibc ≥ 2.34).
- `hik_noi-x86_64` — bản đã dựng.
  Dựng lại 07/10/2026 (cổng 8443, SDK qua TLS) bằng lệnh trên trong container `ubuntu:22.04` sạch (gcc 11.4).
  Dựng lại 09/10/2026 (HIK_NGHE_FD: tiếng mic camera về trong lúc kênh mở) cùng cách, hai lần dựng ra cùng
  sha256 `6bf4c2038d272df694b2dcc481f94d48d15e1b0f525f32f5a8e7b0c2c9371c6c`.
  Rebuilt the same way (numeric SDK error code in `LOI <code> …`, camera-only audio on `HIK_NGHE_FD` via
  byAudioFlag, password wiped from the environment after login): two builds, same sha256
  `f61aaae335be6defb56b2c8f01d472d5e491b206f0153229a433f9adaf0f5f40`.
- `glibc-x86_64/` — trình nạp và thư viện glibc / libstdc++ / libgcc_s / libuuid lấy nguyên từ
  Ubuntu 22.04, để chạy được trong container Home Assistant (Alpine, musl). glibc và libuuid theo
  LGPL-2.1; libstdc++ và libgcc_s theo GPL-3.0 kèm GCC Runtime Library Exception. Mã nguồn: gói
  `glibc`, `gcc-12`, `util-linux` của Ubuntu 22.04 (https://launchpad.net/ubuntu/+source/…).

HCNetSDK (thư viện của Hikvision) KHÔNG đi kèm: tải "Device Network SDK (Linux 64-bit)" ở trang
Hikvision, chép thư mục `lib` vào `/config/hcnetsdk/lib`.
