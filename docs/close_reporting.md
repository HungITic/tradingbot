# Lý do đóng lệnh và PnL từng deal

Lỗi đã sửa: vòng đồng bộ gán reason 3 thành SL và reason 4 thành TP. Theo ENUM_DEAL_REASON của MT5, 3 là Expert/program, 4 là SL, 5 là TP và 6 là Stop Out.

Nguồn: https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties

## Lý do thực hiện

- SL, TP và Stop Out luôn lấy từ mã broker; không suy diễn từ dấu PnL hoặc giá đóng. SL đã dời có thể khớp khi đang có lãi.
- Đối với đóng do bot, lưu ID order/deal mà broker trả về vào nhật ký hành động và đối chiếu chính xác với deal thoát, cùng position ID.
- Phân biệt TP1, TP cuối, tín hiệu chốt entry xấu, tín hiệu đóng/chốt một phần, thao tác Telegram, đóng tất cả và dừng khẩn cấp.
- Có thể khôi phục đối chiếu từ nhật ký DB sau restart. Hành động thất bại không được dùng để giải thích một deal đã khớp.
- Không gán nguyên nhân chỉ vì cùng ticket vị thế, cùng mức giá, hoặc gần nhau về thời gian. Lần chốt TP1 trước không được dùng giải thích một deal thoát khác về sau.
- Thiếu ID hoặc dữ liệu lịch sử cũ không đủ: hiển thị nguồn MT5 (desktop/mobile/web/program) hoặc mã chưa xác định. Không bịa lý do chi tiết.

## PnL và cách hiển thị

- Bỏ cách gọi mọi deal thoát là “Kết thúc PnL”. Báo cáo ghi “PnL lần chốt”, không khẳng định toàn vị thế đã kết thúc.
- Lần chốt được xác định từ hành động chốt một phần sẽ có tiêu đề riêng.
- Hiển thị lãi/lỗ giá, swap, commission và tổng của ba khoản này. Phí của deal vào lệnh chưa được phân bổ vào số này; đây không phải tổng PnL cả kèo hay biến động số dư toàn tài khoản.
- Ghi riêng position ID và deal ID; nguồn tín hiệu được tìm theo vị thế, thay vì ticket của order/deal thoát.
- Deal đảo chiều được gọi là đảo chiều; khối lượng deal có thể gồm cả phần mở lại nên không gán toàn bộ thành “khối lượng đã đóng”.

## Xác minh

211 kiểm thử tự động đạt, bao gồm reason codes, SL có lãi, đối chiếu qua ID, chốt entry xấu, khôi phục sau restart, PnL từng deal và đường gửi thông báo trong vòng đồng bộ. Kiểm thử dùng broker/DB giả lập.

Mã đã sửa nhưng tiến trình giao dịch chưa được khởi động lại; thông báo Telegram đã gửi trước đây không tự được sửa. Không đổi điều kiện mở/đóng lệnh hoặc phát sinh giao dịch thật trong quá trình kiểm tra.
