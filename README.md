# 🤖 Trading Bot V1 — Telegram Signals ➔ Exness MT5

Hệ thống giao dịch tự động V1 chạy trực tiếp trên **macOS Apple Silicon (M4)**, thu thập tín hiệu từ nhóm Telegram A (Userbot Telethon), bóc tách nội dung bằng bộ xử lý quy tắc tiếng Việt, kiểm duyệt qua Risk Engine, và thực thi trực tiếp trên sàn **Exness** thông qua phần mềm **MetaTrader 5 (MT5)**.

Toàn bộ hệ thống được điều khiển, giám sát và cấu hình realtime từ xa thông qua **Personal Telegram Bot**.

---

## ⚡ Bắt Đầu Nhanh (Quick Start)

### 1. Khởi động PostgreSQL
```bash
docker compose up -d postgres
```

### 2. Đăng nhập Telethon Userbot (Lần đầu tiên)
```bash
PYTHONPATH=. .venv/bin/python scripts/login_telethon.py
```

### 3. Chạy Trading Bot
```bash
PYTHONPATH=. .venv/bin/python app/main.py
```

---

## 📖 Tài Liệu Hướng Dẫn & Kịch Bản Test Chi Tiết

👉 **Xem hướng dẫn chi tiết từng bước và các kịch bản test tại:**  
[docs/user_guide.md](file:///Users/itic/Documents/trading-bot/docs/user_guide.md)

Nội dung bao gồm:
1. Checklist chuẩn bị trước khi chạy.
2. Kịch bản test bắn tín hiệu vào lệnh mới từ Telegram Group.
3. Kịch bản test từ khóa tiếng Việt (`chốt bớt`, `dời SL`, `chốt hết`,...).
4. Kịch bản can thiệp, đóng/sửa lệnh trực tiếp từ Personal Bot.
5. Kịch bản cấu hình Risk & thêm từ khóa tiếng Việt mới (`/rules`, `/rule add`).
6. Kịch bản test dừng khẩn cấp (`/kill`, `/pause`).
7. Bảng tra cứu toàn bộ câu lệnh của Personal Bot.
8. Khắc phục các lỗi thường gặp (Retcode 10027, 10016,...).

👉 **Xem hướng dẫn chuyển sang máy khác tại:**  
[docs/migration_guide.md](file:///Users/itic/Documents/trading-bot/docs/migration_guide.md)

---

## 🧪 Chạy Kiểm Thử Tự Động (Unit Tests)
Hệ thống đi kèm bộ kiểm thử tự động 24 test cases kiểm tra Parser, Risk Engine, và Trading Service:
```bash
.venv/bin/pytest tests/ -v
```
