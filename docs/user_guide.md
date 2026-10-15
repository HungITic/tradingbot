# 📘 Hướng Dẫn Sử Dụng & Kịch Bản Test — Trading Bot V1

> **Cập nhật 15/10/2026:** Xem [hướng dẫn nâng cấp](upgrade_2026_10_15.md) cho menu mới, bước xem trước/xác nhận, giới hạn thực thi và chống xử lý trùng. Các ví dụ thao tác bên dưới mô tả V1; những hành vi mới trong hướng dẫn nâng cấp được ưu tiên.

Tài liệu này hướng dẫn chi tiết cách vận hành, cấu hình và thực hiện các kịch bản kiểm thử (Test Cases) toàn diện từ Telegram Group tín hiệu đến MetaTrader 5 (Exness Demo).

---

## 📌 Mục Lục
1. [Kiến Trúc & Luồng Vận Hành](#1-kiến-trúc--luồng-vận-hành)
2. [Checklist Chuẩn Bị Trước Khi Chạy](#2-checklist-chuẩn-bị-trước-khi-chạy)
3. [Bước 1: Đăng Nhập Telethon Userbot (1 lần duy nhất)](#3-bước-1-đăng-nhập-telethon-userbot-1-lần-duy-nhất)
4. [Bước 2: Khởi Động Trading Bot](#4-bước-2-khởi-động-trading-bot)
5. [Kịch Bản Test End-to-End (Từng Bước)](#5-kịch-bản-test-end-to-end-từng-bước)
   - [Test 1: Kiểm Tra Kết Nối & Tài Khoản](#test-1-kiểm-tra-kết-nối--tài-khoản)
   - [Test 2: Test Vào Lệnh Mới (New Order Signal)](#test-2-test-vào-lệnh-mới-new-order-signal)
   - [Test 3: Test Quản Lý Vị Thế Bằng Tiếng Việt (Chốt bớt, Dời SL)](#test-3-test-quản-lý-vị-thế-bằng-tiếng-việt)
   - [Test 4: Can Thiệp Lệnh Thủ Công Qua Personal Bot](#test-4-can-thiệp-lệnh-thủ-công-qua-personal-bot)
   - [Test 5: Cấu Hình Risk & Tinh Chỉnh Từ Khóa Tiếng Việt](#test-5-cấu-hình-risk--tinh-chỉnh-từ-khóa-tiếng-việt)
   - [Test 6: Test Chốt Chặn Rủi Ro & Nút Khẩn Cấp (Emergency)](#test-6-test-chốt-chặn-rủi-ro--nút-khẩn-cấp)
6. [Bảng Tra Cứu Lệnh Personal Bot (Cheat Sheet)](#6-bảng-tra-cứu-lệnh-personal-bot-cheat-sheet)
7. [Khắc Phục Sự Cố Thường Gặp (Troubleshooting)](#7-khắc-phục-sự-cố-thường-gặp-troubleshooting)

---

## 1. Kiến Trúc & Luồng Vận Hành

```
[ Telegram Group A ] (Kênh tín hiệu)
        │
        ▼ (Tin nhắn mới)
[ Signal Listener ] (Telethon Userbot)
        │
        ▼ (Raw text)
[ Signal Parser ] ─── Tách Symbol, Action, Entry, SL, TP, Quản lý lệnh
        │
        ▼ (ParsedSignal)
[ Rule Engine ] ───── Đối chiếu từ khóa tiếng Việt (chốt bớt, dời sl,...)
        │
        ▼
[ Risk Engine ] ───── Kiểm tra: Bot Active? Max Positions? Max Lot? Require SL?
        │
        ▼ (Approved Order)
[ MT5 Executor ] ──── Bắn lệnh qua IPC cục bộ tới MT5 Terminal
        │
        ▼
   [ Exness ]

Song song:
[ Personal Bot ] ──── Nhận thông báo realtime & Cung cấp giao diện quản trị Admin
[ PostgreSQL ]   ──── Lưu vết toàn bộ tín hiệu (audit trail), lệnh và cấu hình
```

---

## 2. Checklist Chuẩn Bị Trước Khi Chạy

Trước khi khởi động bot, hãy đảm bảo các mục sau:
- [x] **MT5 Terminal đang mở**: Phần mềm MetaTrader 5 trên Mac đã đăng nhập tài khoản Exness Demo.
- [x] **Algo Trading màu xanh**: Nút **Algo Trading** trên thanh công cụ của MT5 đã được click (biểu tượng Play màu xanh).
- [x] **PostgreSQL Docker đang chạy**: 
  ```bash
  docker compose ps
  ```
  *(Nếu chưa chạy, chạy lệnh: `docker compose up -d postgres`)*
- [x] **File `.env`**: Đã điền đầy đủ các thông tin:
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`
  - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SIGNAL_CHAT_ID`
  - `MT5_LOGIN`, `MT5_SERVER`

---

## 3. Bước 1: Đăng Nhập Telethon Userbot (1 lần duy nhất)

Telethon cần xác thực tài khoản Telegram của bạn lần đầu để lấy quyền đọc tin nhắn từ Group A:

Mở Terminal tại thư mục project, chạy lệnh:
```bash
PYTHONPATH=. .venv/bin/python scripts/login_telethon.py
```

1. Nhập số điện thoại Telegram của bạn (định dạng quốc tế, ví dụ: `+84912345678`).
2. Nhập mã OTP Telegram gửi về app Telegram của bạn.
3. Nhập mật khẩu 2FA (nếu tài khoản có bật).
4. Script sẽ lưu file `userbot.session` và in ra danh sách chat:
   ```
   Chat ID            | Type         | Title
   ------------------------------------------------------------
   -1001904907955     | Channel      | Group Tín Hiệu 🎯 [TARGET MATCH]
   ```
   *Khi thấy dòng `TARGET MATCH`, tức là Userbot đã kết nối thành công và có quyền đọc tin nhắn từ group mục tiêu.*

---

## 4. Bước 2: Khởi Động Trading Bot

Sau khi đã có file session, bạn khởi động toàn bộ hệ thống bằng một lệnh:
```bash
PYTHONPATH=. .venv/bin/python app/main.py
```

Khi khởi động thành công:
1. Log hiển thị: `database_ready`, `mt5_broker_connected`, `personal_bot_ready`, `signal_listener_ready`.
2. **Personal Bot trên Telegram** sẽ ngay lập tức gửi tin nhắn thông báo:
   > 🚀 **Trading Bot V1 Started Successfully!**  
   > • **MT5 Status:** ✅ Connected  
   > • **Signal Listener:** ✅ Active  
   > • **Risk Status:** 🟢 Bot Active  
   > Send /status or /help to interact.

---

## 5. Kịch Bản Test End-to-End (Từng Bước)

### Test 1: Kiểm Tra Kết Nối & Tài Khoản
**Mục tiêu**: Đảm bảo Personal Bot nhận diện đúng bạn là Admin và đọc được số dư từ Exness qua MT5.

- **Thao tác**: Mở Personal Bot trên Telegram, gửi:
  ```
  /status
  ```
  hoặc
  ```
  /balance
  ```
- **Kết quả kỳ vọng**:
  - Bot phản hồi ngay lập tức hiển thị:
    - Trạng thái Bot: `🟢 ACTIVE`
    - MT5 Broker: `✅ CONNECTED (Server: Exness-MT5Trial14)`
    - Login: Số tài khoản Exness của bạn
    - Balance, Equity, Margin chính xác như đang hiển thị trên MT5.
  - Kèm theo các nút bấm tương tác: `[⏸️ Pause Bot]`, `[🔄 Refresh]`, `[📋 View Positions]`, `[⚙️ Config Risk]`.

---

### Test 2: Test Vào Lệnh Mới (New Order Signal)
**Mục tiêu**: Kiểm tra quy trình từ lúc Group A có tin nhắn -> Parse -> Kiểm duyệt Risk -> Mở lệnh trên MT5 -> Gửi thông báo về Personal Bot.

- **Thao tác**: Dùng tài khoản Telegram của bạn (hoặc nick phụ trong group tín hiệu `-1001904907955`), gửi tin nhắn mẫu sau vào Group:
  ```
  Sell #XAU 4148 - 4160
  SL: 4170
  TP: Trên 200pips
  ```
  *(Lưu ý: Thay giá SL cách giá thị trường hiện tại của Vàng ít nhất 5–10 giá để không bị lỗi broker spread)*

- **Kết quả kỳ vọng**:
  1. Terminal log:
     ```
     [info] signal_message_received chat_id=-1001904907955 text='Sell #XAU 4148 - 4160...'
     [info] risk_check_approved action=SELL symbol=XAUUSD lot=0.01
     [info] order_executed ticket=3918304000 volume=0.01 price=...
     ```
  2. Trên MT5 Terminal: Một lệnh **SELL 0.01 XAUUSDm** được mở với SL là `4170.000`.
  3. Trên Personal Bot: Nhận ngay tin nhắn:
     > ✅ **Order Executed**  
     > Type: SELL XAUUSDm  
     > Ticket: `3918304000`  
     > Volume: 0.01 lot @ 4153.200  
     > SL: 4170.0 | TP: None  

---

### Test 3: Test Quản Lý Vị Thế Bằng Tiếng Việt
**Mục tiêu**: Kiểm tra khả năng nhận diện các từ khóa tiếng Việt để chốt bớt khối lượng và dời SL về Entry.

#### Bước 3A: Test Chốt Bớt (Partial Close)
- **Thao tác**: Gửi tin nhắn vào Group:
  ```
  Kèo vàng chạy rồi chốt bớt nha cả nhà
  ```
- **Kết quả kỳ vọng**:
  - Bot nhận diện từ khóa `chốt bớt`.
  - Tự động đóng 50% khối lượng (hoặc % bạn cài đặt trong `/config`).
  - Nếu bật `auto_breakeven_on_partial`: Bot tự động dời SL của phần còn lại về giá Entry!
  - Personal Bot nhận thông báo:
    > ✅ **Position Management: Close**  
    > Ticket: `3918304000` | Vol: 0.01 lot  
    > Result: Request executed  
    >  
    > ✅ **Auto Breakeven on Partial**  
    > Ticket: `3918304000` | SL: 4153.200  

#### Bước 3B: Test Dời SL về Hòa Vốn (Breakeven)
- **Thao tác**: Gửi tin nhắn vào Group:
  ```
  Dời stoploss về entry kèo vàng
  ```
- **Kết quả kỳ vọng**:
  - Bot nhận diện từ khóa `dời stoploss` -> đổi SL của lệnh Vàng về giá Entry (`open_price`).
  - Personal Bot gửi thông báo:
    > ✅ **Position Management: Move SL to Entry**  
    > Ticket: `3918304000` | New SL: 4153.200  

#### Bước 3C: Test Chốt Hết (Full Close)
- **Thao tác**: Gửi tin nhắn vào Group:
  ```
  Chốt hết vàng nhé
  ```
- **Kết quả kỳ vọng**:
  - Bot đóng toàn bộ các lệnh Vàng đang mở.
  - Vị thế trên MT5 biến mất.

---

### Test 4: Can Thiệp Lệnh Thủ Công Qua Personal Bot
**Mục tiêu**: Người dùng có thể trực tiếp quản lý, can thiệp hoặc sửa lệnh từ điện thoại mà không cần mở MT5.

- **Thao tác 4.1 - Xem danh sách lệnh mở**:
  Gửi `/positions` vào Personal Bot.
  - Bot hiển thị danh sách các lệnh đang chạy kèm PnL xanh/đỏ và các nút:
    `[❌ Close 3918304000]` `[🎯 BE 3918304000]`
- **Thao tác 4.2 - Bấm nút tương tác**:
  - Bấm `[🎯 BE <ticket>]`: SL ngay lập tức nhảy về Entry.
  - Bấm `[❌ Close <ticket>]`: Lệnh được đóng ngay lập tức.
- **Thao tác 4.3 - Lệnh chat trực tiếp**:
  - `/setsl <ticket> 4165`: Cập nhật mức SL mới.
  - `/settp <ticket> 4130`: Cập nhật mức TP mới.
  - `/partial <ticket> 50`: Chốt 50% khối lượng của lệnh.
  - `/closeall`: Đóng toàn bộ các vị thế đang mở trong tài khoản.

---

### Test 5: Cấu Hình Risk & Tinh Chỉnh Từ Khóa Tiếng Việt

#### Bước 5A: Tinh chỉnh thông số Risk
- Gửi `/config` vào Personal Bot để xem cấu hình hiện tại.
- Để thay đổi khối lượng lot mặc định:
  ```
  /config set default_lot 0.02
  ```
- Để thay đổi số lệnh mở tối đa:
  ```
  /config set max_open_positions 3
  ```
- Để chỉnh tỷ lệ chốt bớt mặc định (khi group chỉ báo "chốt bớt"):
  ```
  /config set default_partial_close_percent 40
  ```
  *(Các thay đổi này có hiệu lực ngay lập tức trong bộ nhớ và không làm gián đoạn bot)*

#### Bước 5B: Quản lý từ khóa tiếng Việt (Rules)
- Xem danh sách từ khóa hiện tại:
  ```
  /rules
  ```
- Thêm từ khóa tiếng Việt mới theo văn phong của admin group bạn:
  ```
  /rule add "chốt 1/3" PARTIAL_CLOSE 33
  ```
  hoặc
  ```
  /rule add "về be" BREAKEVEN
  ```
  hoặc
  ```
  /rule add "cắt sạch" CLOSE
  ```
- Xóa từ khóa:
  ```
  /rule del "chốt lời"
  ```
- *Kiểm tra: Mọi từ khóa thêm/xóa đều được lưu ngay vào bảng `management_rules` trong PostgreSQL và tự động nạp lại khi bot khởi động.*

---

### Test 6: Test Chốt Chặn Rủi Ro & Nút Khẩn Cấp

#### Bước 6A: Test Tạm Dừng Bot (Pause / Resume)
1. Gửi `/pause` vào Personal Bot -> Bot chuyển trạng thái sang `⏸️ PAUSED`.
2. Bắn một tín hiệu vào Group A:
   ```
   Buy #XAU 4150 - 4155
   SL: 4140
   ```
3. **Kết quả kỳ vọng**: Lệnh **KHÔNG ĐƯỢC MỞ** trên MT5. Personal Bot gửi cảnh báo:
   > ⚠️ **Signal Rejected**  
   > Action: BUY XAUUSD  
   > Reason: [bot_enabled] Bot trading is currently paused or disabled  
4. Gửi `/resume` để kích hoạt lại bot bình thường.

#### Bước 6B: Test Chốt Chặn Bắt Buộc Có SL (Require SL)
1. Bắn một tín hiệu không có SL:
   ```
   Buy #XAU 4150
   TP: 4180
   ```
2. **Kết quả kỳ vọng**: Risk Engine từ chối lệnh với lý do `[require_sl] Signal is missing Stop Loss`. Lệnh không vào MT5.

#### Bước 6C: Nút Dừng Khẩn Cấp (Emergency Kill Switch)
- **Thao tác**: Gửi `/kill` vào Personal Bot.
- **Kết quả kỳ vọng**:
  1. Bot ngay lập tức chuyển sang chế độ vô hiệu hóa (`enabled=False`).
  2. Tự động đóng **TOÀN BỘ** các vị thế đang mở trên MT5.
  3. Gửi thông báo xác nhận:
     > 🚨 **EMERGENCY KILL SWITCH ACTIVATED**  
     > • Bot disabled.  
     > • Closed X active positions.  

---

## 6. Bảng Tra Cứu Lệnh Personal Bot (Cheat Sheet)

| Nhóm | Lệnh | Mô tả chi tiết |
|---|---|---|
| **Giám sát** | `/status` | Xem tổng quan trạng thái bot, kết nối MT5, số dư, equity, margin |
| | `/positions` | Xem danh sách các lệnh đang chạy kèm nút tương tác (Close, BE) |
| | `/balance` | Xem chi tiết số dư, equity, đòn bẩy, server |
| | `/health` | Kiểm tra kết nối MT5, Database, bộ lắng nghe |
| **Quản lý lệnh** | `/close <ticket>` | Đóng một lệnh cụ thể theo mã ticket |
| | `/closeall` | Đóng toàn bộ các vị thế đang mở |
| | `/partial <ticket> <%>` | Chốt một phần vị thế (VD: `/partial 12345 50`) |
| | `/setsl <ticket> <price>` | Đặt mức Stop Loss mới (VD: `/setsl 12345 4150.5`) |
| | `/settp <ticket> <price>` | Đặt mức Take Profit mới (VD: `/settp 12345 4170.0`) |
| | `/breakeven <ticket>` | Dời SL về đúng giá mở lệnh (Entry) |
| **Quản lý từ khóa** | `/rules` | Xem toàn bộ từ khóa tiếng Việt đang dùng |
| | `/rule add <kw> <action> [val]` | Thêm/sửa từ khóa (VD: `/rule add "cắt hòa" BREAKEVEN`) |
| | `/rule del <kw>` | Xóa từ khóa khỏi hệ thống và database |
| | `/rules reload` | Nạp lại danh sách từ khóa từ database PostgreSQL |
| **Cấu hình** | `/config` | Xem toàn bộ cấu hình Risk & Quản lý vị thế |
| | `/config risk` | Mở menu nút bấm trực quan để cấu hình nhanh |
| | `/config set <key> <val>` | Sửa thông số (VD: `/config set max_lot 0.05`) |
| **Khẩn cấp** | `/pause` | Tạm dừng nhận lệnh từ tín hiệu mới |
| | `/resume` | Bật lại chế độ giao dịch tự động |
| | `/kill` | Tắt bot lập tức và đóng toàn bộ vị thế đang mở |

---

## 7. Khắc Phục Sự Cố Thường Gặp (Troubleshooting)

### 1. MT5 báo lỗi `Retcode 10027: AutoTrading disabled`
- **Nguyên nhân**: Bạn chưa kích hoạt giao dịch tự động trên phần mềm MT5.
- **Cách xử lý**: Nhìn lên thanh công cụ của MT5, click vào nút **Algo Trading** sao cho biểu tượng chuyển sang màu xanh lá cây.

### 2. MT5 báo lỗi `Retcode 10016: Invalid stops`
- **Nguyên nhân**: Mức Stop Loss (SL) bạn đặt quá gần với giá thị trường hiện tại (vi phạm khoảng cách tối thiểu / Spread của Exness đối với cặp Vàng).
- **Cách xử lý**: Đảm bảo khoảng cách SL cách giá hiện tại tối thiểu 5–10 USD (ví dụ: Vàng giá 4150 thì Sell SL nên từ 4160 trở lên, Buy SL từ 4140 trở xuống).

### 3. Lỗi `ModuleNotFoundError: No module named 'app'`
- **Cách xử lý**: Luôn thêm biến môi trường `PYTHONPATH=.` khi chạy lệnh:
  ```bash
  PYTHONPATH=. .venv/bin/python app/main.py
  ```

### 4. Lỗi kết nối Database `connection refused`
- **Cách xử lý**: Kiểm tra Docker container PostgreSQL:
  ```bash
  docker compose ps
  docker compose up -d postgres
  ```

### 5. Muốn chạy bot nền liên tục (Background) trên Mac
Để bot tiếp tục chạy ngay cả khi tắt cửa sổ Terminal:
```bash
nohup .venv/bin/python app/main.py > trading_bot.log 2>&1 &
```
Để theo dõi log:
```bash
tail -f trading_bot.log
```
Để dừng bot chạy nền:
```bash
pkill -f "app/main.py"
```
