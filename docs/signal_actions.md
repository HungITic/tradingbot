# Đóng vị thế và hủy lệnh chờ theo signal

Gửi `/setups` hoặc mở **Kèo đang chạy**. Mỗi kèo đã liên kết signal có các nút:

- **Đóng kèo**: hủy các lệnh chờ trước, sau đó đóng toàn bộ khối lượng còn lại
  của các vị thế thuộc signal đó, gồm cả các phần TP1/TP2.
- **Hủy chờ**: chỉ hủy lệnh còn chờ; phần đã khớp thành vị thế được giữ nguyên.

Cả hai thao tác đều có xem trước và xác nhận. Bản xem trước ghi tài khoản,
signal gốc, số vị thế/lệnh chờ, ticket và khối lượng. Các vòng reentry có signal
thực thi riêng được hiển thị thành kèo riêng.

Phạm vi lấy từ liên kết Order → Signal đã lưu, kết hợp Position hoặc deal mở
lệnh để phân biệt ticket vị thế và ticket order. Không gộp theo symbol hay chiều
BUY/SELL. Nhóm chưa liên kết hoặc có dữ liệu liên kết xung đột không có nút đóng
theo signal; vẫn có thể kiểm tra và quản lý riêng từng ticket.

Khi xác nhận, bot kiểm tra lại tài khoản và trạng thái MT5. Nếu kèo có thêm lệnh
hoặc tăng khối lượng so với bản xem trước thì cần xem trước lại. Lệnh đã đóng
hoặc đã hủy được bỏ qua. Với **Đóng kèo**, lệnh chờ trong phạm vi đã duyệt vừa
khớp thành vị thế cũng được nhận diện bằng order/deal để đóng. Với **Hủy chờ**,
vị thế này được giữ nguyên.

Yêu cầu hết hạn sau 5 phút. Nút xác nhận được tiêu thụ một lần; khởi động lại
bot thì cần mở lại kèo. Các thao tác dùng cùng khóa thực thi với luồng signal.
Trước khi gửi yêu cầu thay đổi MT5, bot commit phạm vi vào RiskEvent; kết quả
đóng/hủy được lưu thành ManagementAction. Lỗi gửi không rõ kết quả sẽ dừng các
yêu cầu tiếp theo, không tự gửi lại. Việc hủy/đóng nhiều lệnh trên MT5 là tuần tự,
không thể rollback các yêu cầu đã được sàn thực hiện nếu yêu cầu sau bị lỗi.

Chức năng giảm/đóng vị thế vẫn dùng được khi bot đang tạm dừng mở lệnh mới.
Không cần migration database mới.
