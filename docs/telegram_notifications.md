# Hàng đợi thông báo Telegram

Thông báo từ `TradingService.notify` được callback của bot cá nhân ghi vào bảng
`telegram_notifications` rồi trả về. Action MT5 chỉ chờ transaction DB này, không
chờ HTTP Telegram. Worker riêng gửi các tin tuần tự, không giữ `_execution_lock`
và không gọi hoặc gửi lại action MT5. Các tin trả lời tương tác `/status`, xem
trước/xác nhận và callback Telegram vẫn dùng luồng trả lời hiện có.

Ví dụ: mở Min Entry → lưu thông báo → đặt Max Entry; chốt một phần → lưu thông báo
→ dời SL. Telegram chậm hoặc mất mạng không nằm giữa các bước giao dịch này.

## Lưu trữ và thử lại

- Mỗi tin có text HTML, inline keyboard, bot key băm (không lưu token), admin chat,
  trạng thái `PENDING/SENDING/SENT/FAILED`, số lần thử và thời điểm được thử tiếp.
  Worker chỉ đọc đúng bot/admin đã cấu hình, tránh gửi hàng đợi của bot khác hoặc
  admin cũ sang người nhận mới.
- Worker khóa bản ghi đầu hàng đợi, commit lease `SENDING` 60 giây trước khi gọi
  Telegram, rồi ghi kết quả sau khi gửi. Mỗi lần gửi có timeout tổng 20 giây. Khi
  bot dừng giữa chừng, bản ghi chưa hoàn tất được lấy lại sau khi lease hết hạn.
  Claim token ngăn sender cũ ghi đè trạng thái của lần xử lý mới.
- Lỗi mạng/timeout được thử lại với thời gian chờ tăng dần, tối đa 60 giây giữa
  các lần thử. `RetryAfter` dùng đúng khoảng chờ Telegram trả về. Tin phía sau
  không vượt qua tin đang backoff. Lỗi payload/quyền gửi (`BadRequest/Forbidden`)
  chuyển `FAILED`, ghi error log và tiếp tục tin sau; bản ghi lỗi được giữ để kiểm tra.
- Tin `SENT` quá 3 ngày được dọn định kỳ mỗi giờ. Tin chưa gửi, đang gửi hoặc lỗi
  không bị xóa bởi tác vụ này. Log file vẫn dùng chính sách chỉ ERROR/giữ 3 ngày.

Đảm bảo giao nhận là **at least once**: nếu Telegram nhận tin nhưng tiến trình
dừng hoặc mất kết nối trước khi lưu acknowledgement, tin có thể xuất hiện lại.
Việc gửi lại thông báo không thực thi lại giao dịch. Outbox chỉ bảo vệ những tin
đã commit vào hàng đợi; ghi kết quả giao dịch và enqueue thông báo hiện là hai
transaction riêng. Nếu enqueue lỗi, service ghi ERROR; journal giao dịch vẫn
được giữ để đối soát, không tự gửi lại lệnh MT5.

## Khởi động và tắt

Worker bắt đầu sau khi transport Telegram sẵn sàng, tự đọc hàng đợi còn tồn từ
trước. Shutdown dừng producer theo lifecycle cũ, cho worker tối đa 5 giây để gửi
nốt rồi hủy task nếu đang chờ mạng. Transport Telegram chỉ shutdown sau worker;
tin chưa gửi vẫn ở DB cho lần khởi động sau. Nút duyệt cũ còn trong hàng đợi sau
restart vẫn phải qua kiểm tra hiệu lực hiện có; worker không khôi phục approval
đã mất khỏi bộ nhớ.

Migration `e5f6a7b8c9d0` thêm bảng hàng đợi và index, không sửa dữ liệu giao dịch.
Startup `init_db` hiện có cũng tự tạo bảng này qua `Base.metadata.create_all`, nên
cập nhật code và restart bằng `run.py`/daemon hiện có sẽ tự tạo phần schema còn
thiếu. Luồng triển khai dùng Alembic có thể chạy `.venv/bin/alembic upgrade head`.

Kiểm thử PostgreSQL dùng database dùng một lần `codex_acid_*`, không dùng DB giao
dịch. Các test khác mô phỏng HTTP chậm, timeout, rate limit, lifecycle, serializing
keyboard và việc chốt một phần/dời SL hoàn tất khi worker Telegram đang bị treo.
