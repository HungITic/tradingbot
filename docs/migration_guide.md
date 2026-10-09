# 🚀 Hướng Dẫn Chuyển & Cài Đặt Trading Bot Sang Máy Khác (Cùng macOS Apple Silicon)

Tài liệu này hướng dẫn cách chuyển toàn bộ Trading Bot V1 sang một máy Mac khác (MacBook M1/M2/M3/M4, Mac mini, Mac Studio) để chạy mượt mà ngay lập tức.

---

## 📌 Checklist Điều Kiện Cần Trên Máy Mới

Đảm bảo máy mới đã cài sẵn 3 ứng dụng nền tảng:
1. **MetaTrader 5 cho macOS**:
   - Cài đặt bản MT5 chính thức của MetaQuotes cho Mac (ứng dụng nằm tại `/Applications/MetaTrader 5.app`).
   - Đăng nhập tài khoản Exness Demo.
   - Bật nút **Algo Trading** (chuyển sang màu xanh lá).
2. **Docker Desktop for Mac**:
   - Cài đặt và mở Docker Desktop.
3. **Python 3.11 hoặc 3.12**:
   - Khuyến nghị cài qua Homebrew:
     ```bash
     brew install python@3.12
     ```

---

## ⚡ Lựa Chọn 1: Copy Toàn Bộ Thư Mục (Nhanh Nhất — Không Cần Nhập Lại OTP)

Nếu bạn copy thư mục `trading-bot` từ máy cũ sang máy mới (bằng AirDrop, USB, Google Drive, hoặc nén `.zip`):

### Bước 1: Những file quan trọng cần mang theo
- Thư mục mã nguồn: `app/`, `scripts/`, `migrations/`, `docker-compose.yml`, `requirements.txt`...
- File cấu hình: `.env` *(chứa token bot, chat ID, MT5 server...)*
- File phiên đăng nhập: `userbot.session` *(giữ file này thì **không cần nhận mã OTP Telegram lại**)*

*(Lưu ý: Không cần copy thư mục `.venv` vì ta sẽ tạo mới trên máy mới để tránh xung đột đường dẫn)*

### Bước 2: Chạy Docker PostgreSQL trên máy mới
Mở Terminal tại thư mục project:
```bash
docker compose up -d postgres
```

### Bước 3: Tạo môi trường ảo & cài thư viện
```bash
# 1. Tạo venv
python3 -m venv .venv
source .venv/bin/activate

# 2. Cài đặt các thư viện đã được lock phiên bản
pip install -r requirements.txt

# 3. Chạy script tự động patch mt5_mac cho chip Apple Silicon
python scripts/patch_mt5_mac.py
```

### Bước 4: Khởi tạo dữ liệu Database
```bash
# Áp dụng migration bảng
.venv/bin/alembic upgrade head

# Nạp cấu hình mặc định và từ khóa tiếng Việt
PYTHONPATH=. .venv/bin/python scripts/seed_config.py
```

### Bước 5: Chạy kiểm thử tự động (Optional)
Kiểm tra đảm bảo 24/24 test cases hoạt động bình thường:
```bash
.venv/bin/pytest tests/ -v
```

### Bước 6: Khởi động Trading Bot
```bash
PYTHONPATH=. .venv/bin/python app/main.py
```
*Vì đã có sẵn file `userbot.session` từ máy cũ, bot sẽ kết nối ngay lập tức mà không hỏi số điện thoại hay mã OTP!*

---

## 🌐 Lựa Chọn 2: Cài Đặt Từ Git (Clean Setup)

Nếu bạn đưa code lên GitHub/GitLab và clone về máy mới:

```bash
# 1. Clone repository
git clone <URL_REPO> trading-bot
cd trading-bot

# 2. Tạo file .env từ file mẫu và điền thông tin tài khoản
cp .env.example .env
nano .env

# 3. Bật database
docker compose up -d postgres

# 4. Cài đặt môi trường & thư viện
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 5. Patch mt5_mac cho Apple Silicon
python scripts/patch_mt5_mac.py

# 6. Khởi tạo Database
.venv/bin/alembic upgrade head
PYTHONPATH=. .venv/bin/python scripts/seed_config.py

# 7. Đăng nhập Telegram Userbot lần đầu (nhập SĐT & mã OTP)
PYTHONPATH=. .venv/bin/python scripts/login_telethon.py

# 8. Khởi động bot
PYTHONPATH=. .venv/bin/python app/main.py
```

---

## 🛠️ Lưu Ý Quan Trọng Khi Đổi Máy

1. **MT5 Path & Server**:
   - Đảm bảo tên file MT5 trên máy mới là `/Applications/MetaTrader 5.app`.
   - Đăng nhập đúng server (ví dụ `Exness-MT5Trial14`) và tài khoản demo.
2. **Chạy bot nền liên tục (Background) trên máy mới**:
   ```bash
   nohup .venv/bin/python app/main.py > trading_bot.log 2>&1 &
   ```
   Để kiểm tra log đang chạy:
   ```bash
   tail -f trading_bot.log
   ```
   Để dừng bot chạy nền:
   ```bash
   pkill -f "app/main.py"
   ```
3. **Chỉ chạy 1 máy tại một thời điểm**:
   - Không chạy bot đồng thời trên cả máy cũ và máy mới với cùng 1 tài khoản Telegram Userbot và cùng 1 Token Bot để tránh bị xung đột phiên đăng nhập (Conflict / Polling collision). Hãy tắt bot ở máy cũ trước khi bật máy mới.
