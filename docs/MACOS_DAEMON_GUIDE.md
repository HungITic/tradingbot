# Hướng Dẫn Duy Trì Trading Bot Chạy 24/7 Trên macOS

Tài liệu này hướng dẫn cách cấu hình và duy trì Trading Bot hoạt động bền bỉ, liên tục 24/7 trên macOS mà **không bị tắt khi đóng Terminal**, **tự động khởi động lại khi crash hoặc mở máy**, và **ngăn macOS tự động Sleep làm mất kết nối sàn MT5**.

---

## 1. Cơ Chế Hoạt Động

Hệ thống sử dụng 2 cơ chế chuẩn của macOS:
1. **macOS `launchd` (LaunchAgent):**
   - Đăng ký service `com.tradingbot` trong hệ thống macOS.
   - `RunAtLoad = true`: Tự động khởi chạy ngay khi bạn đăng nhập vào máy.
   - `KeepAlive = true`: Nếu bot gặp sự cố tắt hoặc crash, macOS tự động bật lại bot sau 5 giây.
   - Chạy nền độc lập: Bạn có thể tắt Terminal, Antigravity IDE hay VS Code mà bot vẫn hoạt động bình thường.
2. **`caffeinate -ims`:**
   - Ngăn macOS đưa hệ thống vào trạng thái Sleep hoặc ngắt kết nối mạng WiFi/Ethernet khi bạn không chạm vào chuột/bàn phím.

---

## 2. Cách Sử Dụng Qua Script Quản Lý

Chúng tôi đã tạo sẵn script quản lý 1 chạm [`scripts/bot.sh`](file:///Users/itic/Documents/trading-bot/scripts/bot.sh):

```bash
# 1. Bật bot chạy nền (Tự động đăng ký LaunchAgent và khởi chạy)
./scripts/bot.sh start

# 2. Kiểm tra trạng thái bot đang chạy
./scripts/bot.sh status

# 3. Xem log thời gian thực
./scripts/bot.sh logs

# 4. Xem log lỗi (nếu có)
./scripts/bot.sh errors

# 5. Khởi động lại bot
./scripts/bot.sh restart

# 6. Dừng bot hoàn toàn
./scripts/bot.sh stop

# 7. Gỡ bỏ hoàn toàn service khỏi macOS
./scripts/bot.sh uninstall
```

---

## 3. Cài Đặt Hệ Thống macOS Quan Trọng

Đối với máy MacBook (xách tay), khi không sử dụng bạn cần lưu ý:

### A. Chế độ gập màn hình (Clamshell Mode)
Khi bạn gập màn hình MacBook lại:
1. **Bắt buộc phải cắm sạc nguồn (AC Power)**. Nếu chạy bằng pin, mặc định macOS sẽ luôn cưỡng chế Sleep khi gập màn hình.
2. Khuyến nghị cài đặt ứng dụng miễn phí **[Amphetamine](https://apps.apple.com/app/amphetamine/id937984704)** trên Mac App Store:
   - Trong app Amphetamine: Tích chọn *"Allow system sleep when display is closed"* = **OFF**.

### B. Cài đặt Cài đặt hệ thống (System Settings)
1. Mở **Cài đặt hệ thống (System Settings)** $\rightarrow$ **Màn hình khóa (Lock Screen)**:
   - Chọn *"Tắt màn hình khi không hoạt động"* (Turn display off when inactive) $\rightarrow$ Tùy ý (ví dụ: 10 phút để tiết kiệm màn hình).
2. Vào **Màn hình (Displays)** $\rightarrow$ **Nâng cao (Advanced)**:
   - Bật tùy chọn: *"Ngăn tự động ngủ khi màn hình tắt"* (Prevent automatic sleeping when display is turned off).
3. Đảm bảo kết nối mạng Wi-Fi hoặc cáp LAN luôn ổn định.

---

## 4. Quản Lý File Log

Bot chỉ ghi **ERROR và CRITICAL**, kể cả thư viện Telegram/Telethon và SQLAlchemy. Không bật SQL echo khi `DEBUG=true`; `LOG_LEVEL=INFO/DEBUG` trong cấu hình cũ không làm phát sinh log thông thường.

- `logs/bot_error.log`: log lỗi hiện tại, gồm traceback. Khi chạy daemon, lỗi xảy ra trước khi app khởi tạo cũng được thu lại.
- Xoay file lúc nửa đêm theo múi giờ máy chạy bot; giữ file hiện tại và tối đa hai file ngày trước, tổng cộng ba ngày log. Log cũ quá 72 giờ được dọn thêm theo thời gian sửa file.
- Dọn lúc khởi động và kiểm tra mỗi phút khi bot chạy, kể cả khi không có lỗi mới. `bot.log` cũ quá hạn cũng được dọn; không ghi thêm log hoạt động vào file này.
- `./scripts/bot.sh logs` và `./scripts/bot.sh errors` đều theo dõi file lỗi, tự tiếp tục khi file được xoay.
- Chạy trực tiếp `.venv/bin/python run.py` vẫn ghi cùng file lỗi. Khi không có lỗi, Terminal có thể không xuất log.
