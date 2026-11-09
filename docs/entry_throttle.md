# Chặn mở kèo liên tục

Trong `/config` → **Chặn vào liên tục** có ba tham số:

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `entry_cooldown_seconds` | `0` | Khoảng nghỉ tối thiểu giữa hai lượt mở kèo; 0 tắt chặn khoảng nghỉ |
| `max_entries_per_window` | `0` | Số lượt mở kèo tối đa trong cửa sổ trượt; 0 tắt chặn số lượng |
| `entry_window_seconds` | `300` | Độ dài cửa sổ đếm, tính bằng giây và phải lớn hơn 0 |

Ví dụ nghỉ ít nhất 60 giây giữa hai kèo và tối đa 3 kèo trong bất kỳ 5 phút:

```text
/config set entry_cooldown_seconds=60 max_entries_per_window=3 entry_window_seconds=300
```

Bot tạo bản xem trước thay đổi để xác nhận như các cấu hình hiện có. Có thể dùng
riêng một chặn, hoặc kết hợp cả hai. Mặc định hai chặn tắt cho đến khi đặt ngưỡng.

## Phạm vi

Áp dụng chung trên tài khoản MT5 trong cùng database, cho mọi symbol, BUY/SELL,
signal mới, vòng mới/reentry, `/replay` và lệnh được duyệt qua nút Telegram.
Các chặn này không có nút duyệt vượt ngưỡng. Giới hạn vị thế, lọc trùng, lỗ ngày
và các kiểm tra còn lại vẫn hoạt động.

Một signal được cấp lượt mở một lần: Min/Max Entry và các phần TP1/TP2 của nó
cùng sử dụng lượt đó. Đặt limit được tính ngay khi gửi kèo, không tính lại khi
limit khớp sau này. Chốt/hủy lệnh hoặc đóng sớm không trả lại lượt trong cửa sổ.
Các lệnh đặt thủ công bên ngoài bot không nằm trong bộ đếm này.

Khi chạm ngưỡng, bot thông báo lý do, thời gian cần đợi và từ chối signal. Không
tự xếp hàng để thực thi muộn. Chốt/hủy/dời SL của vị thế hiện tại không bị chặn.
Signal ngược chiều bị chặn bởi giới hạn này sẽ không chạy bước chốt nửa vị thế
ngược chiều để mở kèo mới. Các cập nhật quản lý/hủy limit liên quan vẫn theo
chính sách riêng hiện có.

## Lưu lịch sử và thực thi

Khi bật ít nhất một chặn, trước yêu cầu mở MT5 đầu tiên của kèo, bot cấp và
commit lượt vào bảng `entry_admissions`. Thời gian dùng đồng hồ database, khóa
transaction theo tài khoản để kiểm tra và cấp lượt cùng lúc. Index tài khoản/
thời gian phục vụ cửa sổ trượt; mỗi signal chỉ được cấp một lượt trên tài khoản.
Các lượt còn hiệu lực được đọc lại sau restart và khi thay đổi ngưỡng.

Lượt đã cấp vẫn được tính nếu sàn từ chối, trả kết quả chưa rõ, hoặc tiến trình
dừng giữa lúc commit lượt và gửi MT5. Cách tính này ngăn thử gửi liên tục khi có
sự cố. Chưa đến bước cấp lượt (lỗi parse, chờ duyệt, không đủ lot...) thì chưa
tính lượt. Không kiểm tra/ghi được lịch sử khi đang bật chặn thì dừng mở mới.

Khi cả hai chặn đều tắt, bot không cấp/ghi lượt mới. Lịch sử trước lần đầu bật
tính năng không được suy đoán từ vị thế hoặc giao dịch thủ công. Bật lại sẽ dùng
các lượt đã lưu còn nằm trong cửa sổ, cùng những lượt mới từ lúc bật lại.

Startup `init_db()` tự tạo bảng còn thiếu khi chạy mã mới, nên restart bình
thường tự bổ sung bảng. Có migration Alembic `f6a7b8c9d0e1` cho quy trình dùng
Alembic; chạy sau startup cũng an toàn vì migration kiểm tra bảng đã tồn tại.
