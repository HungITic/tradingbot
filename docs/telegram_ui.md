# Giao diện Telegram gọn

Các khối nội dung cách nhau một dòng trống, không dùng đường kẻ dài.
Vị thế hiển thị bốn dòng; lệnh chờ ba dòng. PnL đang chạy và PnL đã chốt
có nhãn riêng. Giá giữ độ chính xác từ dữ liệu; số tiền hiển thị hai số lẻ.

## Lịch sử

Mỗi trang ba vị thế, sắp theo lần chốt gần nhất trước (theo dữ liệu service).
Không còn giới hạn chỉ xem được 15 vị thế gần nhất. Ví dụ một mục:

```text
XAUUSD.sc · SELL · #343575690
PnL đã chốt +896.10 USD · Đóng 0.75/0.75 lot
Entry 4,161.12 → Đóng TB 4,149.17
10:30 16/10/2026 · Đã đóng hết · 2 lần chốt
```

Nút chi tiết theo ticket mở phần phí và từng lần chốt. Mỗi trang chi tiết
có tối đa năm lần chốt, mới nhất trước. Nút quay lại giữ kỳ lọc và trang danh sách.
Nút “Mới hơn”, “Cũ hơn”, “Lệnh mới nhất” giúp di chuyển mà không cuộn tin dài.
Đổi kỳ lọc về trang đầu; cập nhật giữ trang hiện tại và tự giới hạn khi số trang giảm.
Các nút lịch sử cũ `history_7`, `history_30` vẫn dùng được.

Bộ lọc lịch sử gồm “Hôm nay”, “Hôm qua”, “Tháng này”; mặc định là “Hôm nay”.
Các mốc đều theo giờ Việt Nam: hôm nay từ 00:00 đến hiện tại, hôm qua từ
00:00 hôm qua đến trước 00:00 hôm nay, tháng này từ ngày 1 đến hiện tại.
Khoảng lấy dư của MT5 được lọc lại theo thời điểm từng deal chốt, trước khi cộng
lot, PnL và phí trong kỳ. Các deal mở trước kỳ vẫn có thể cung cấp giá entry,
nhưng phí mở ngoài kỳ không cộng vào PnL của kỳ đang xem.
Chuyển trang, xem chi tiết và quay lại giữ nguyên bộ lọc. Các nút cũ và
`/history <số ngày>` vẫn dùng được với khoảng ngày trượt được lọc đúng mốc.
Lịch sử gồm cả vị thế đã chốt một phần.

## Các màn hình còn lại

Tổng quan gộp kết nối, số vị thế/lệnh chờ và tình trạng chờ duyệt thành các dòng ngắn.
Khối lượng tháng giữ ba số mở, đóng, tổng hai chiều trên toàn tài khoản.
Hiệu suất gộp số lệnh thắng/thua/hòa trên cùng dòng. Kèo, hộp thư, cài đặt,
xem trước và thông báo dùng khoảng cách chung; thông báo vào lệnh gộp lot và giá.
