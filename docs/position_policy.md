# Chính sách quản lý kèo thống nhất — 15/10/2026

Bản này thay thế các mô tả cũ về TP1, hòa vốn và chốt một phần trong hướng dẫn V1. Các chính sách nằm trong `app/trading/position_policy.py`, dùng chung cho tín hiệu, command, menu và tác vụ kiểm tra TP.

## Hòa vốn và SL

- SL/TP lúc mở lệnh được bù **0.5 giá** so với mức trong tín hiệu: BUY trừ 0.5; SELL cộng 0.5. TP được kéo gần entry, SL được đẩy xa entry. Áp dụng cả TP1/TP2 và cả hai nhánh entry, gồm lệnh chờ và lệnh duyệt vượt giá. Ví dụ TP 4100 → BUY 4099.5, SELL 4100.5; SL 4200 → BUY 4199.5, SELL 4200.5.
- Giá entry giữ nguyên; màn hình xem trước hiển thị SL/TP sau bù, nhật ký tín hiệu giữ mức gốc. Nếu TP sau bù nằm sai phía entry/giá khớp thì từ chối đặt lệnh. Quy tắc bù áp dụng khi mở lệnh mới, không tự sửa vị thế đang chạy hoặc lệnh dời SL/hòa vốn.
- Hòa vốn luôn dùng **entry riêng của từng ticket**. Quy tắc lấy trung bình hai entry đã bị loại bỏ trong mọi luồng: tín hiệu, TP1, command và nút BE.
- Ví dụ: lệnh 1 vào 2650, lệnh 2 vào 2640 → SL hòa vốn lần lượt là 2650 và 2640 khi offset bằng 0. Khối lượng hai lệnh không làm thay đổi điểm hòa vốn. Đây là hòa vốn theo giá, chưa tính commission/swap.
- Offset được cộng với BUY, trừ với SELL; offset phải không âm.
- SL tự động mặc định chỉ cải thiện mức bảo vệ. Nếu SL hiện tại tốt hơn thì giữ nguyên, thông báo không thay đổi. Đích SL phải đúng phía quote mới; lỗi sửa SL không được báo là thành công.
- Nút BE cho xem trước giá SL và chính sách rồi xác nhận. Nếu giá hòa vốn tính lại đã thay đổi thì yêu cầu xem trước lại. Chỉ ticket được chọn bị sửa.
- `/breakeven <ticket>` vẫn là lệnh thực thi trực tiếp, nhưng trả giá SL thực tế và kết quả.
- Tin `XAUUSD dời SL về 2662` dùng đúng giá 2662; không tự đổi thành hòa vốn. Chỉ thị giá đích rõ ràng được xem là can thiệp chủ động, không bị ràng buộc bởi lựa chọn “chỉ cải thiện” của SL tự động.

## TP và chốt một phần

- Có TP1 và TP2: TP2 là TP cuối gắn trên MT5. Khi TP1 được chạm, bot chốt theo `default_partial_close_percent`, rồi dời SL nếu bật `auto_breakeven_on_partial`.
- Chỉ có một TP: đóng toàn bộ tại TP đó. Bot không đồng thời chạy chốt một phần để cạnh tranh với TP trên MT5.
- Chốt một phần luôn dùng `volume_min` và `volume_step` của broker, làm tròn xuống. Không đủ khối lượng hoặc phần dư nhỏ hơn lot tối thiểu thì không tự đóng hết vị thế.
- Khi TP1 thiếu lot, có hai lựa chọn: bỏ qua hoặc chỉ dời SL nếu đã bật tự dời. Tắt tự dời SL thì TP1 cũng không tự dời.
- Chốt TP1 thất bại: không dời SL, không đánh dấu đã xử lý thành công.
- Chốt thành công nhưng dời SL thất bại: không chốt lại; chỉ thử lại bước dời SL ở lần kiểm tra đủ điều kiện kế tiếp.
- Lịch sử hành động lưu ticket trong JSON để tra cứu kể cả khi chưa có bản ghi Position. Khi khởi động lại, bot kiểm tra lịch sử và khối lượng còn lại để tránh chốt lặp.
- `allow_multiple_partial_closes` áp dụng chung cho chốt thủ công, menu và tín hiệu; TP1 tự động luôn xử lý một lần. Khi bật chốt nhiều lần, người dùng có thể chủ động chốt thêm.
- Các câu chung như “chốt bớt” mặc định dùng tỷ lệ cấu hình chung, kể cả rule DB cũ còn mang giá trị 50%. Tỷ lệ ghi rõ trong tin nhắn luôn được ưu tiên. Rule riêng như “chốt một phần ba” vẫn giữ tỷ lệ riêng. Có thể tắt kế thừa tỷ lệ chung bằng `generic_partial_uses_default=false`.

## Chốt bớt entry xấu → chỉ Min Entry

- Tin có cụm **“chốt bớt” và từ “xấu”** được nhận diện là chốt một phần **Min Entry**. Ví dụ: “chốt bớt entry xấu”, “chốt bớt lệnh xấu”, “entry xấu thì chốt bớt”. Từ `XAU` (symbol vàng) không bị hiểu thành “xấu”.
- Mặc định dùng `default_partial_close_percent`, đúng lựa chọn của người dùng. Nếu tin ghi rõ `30%` thì dùng 30%. Không đóng toàn bộ Min Entry chỉ vì có từ “xấu”; Max Entry không bị chốt.
- Ví dụ: Min Entry còn 0.10 lot, Max Entry còn 0.10 lot, tỷ lệ cấu hình 40% → chỉ chốt 0.04 lot của Min Entry, để lại 0.06 lot Min và 0.10 lot Max, tùy bước lot broker.
- Chọn nhánh bằng nhãn đã lưu lúc đặt lệnh: `bot-min-entry`/`bot-override-min`; dùng nhãn broker khi thiếu nhãn DB. Không suy đoán Min bằng giá thấp/cao hoặc SL hiện tại. Nhãn DB và broker mâu thuẫn thì không tự chọn.
- Reply giới hạn vào đúng kèo. Nếu không có symbol/reply, chỉ xử lý khi có duy nhất một kèo Min Entry xác định. Có nhiều kèo phù hợp thì yêu cầu reply hoặc chỉ rõ “tất cả”.
- Không có Min Entry đang mở, nhãn không đủ rõ hoặc lot không đủ chia: không chuyển sang chốt Max Entry hoặc đóng hết lệnh tối thiểu.
- Nếu bật tự dời SL sau chốt, chỉ Min Entry được tự dời về entry riêng của nó. Tin có thêm chỉ thị “dời SL” riêng vẫn áp dụng chỉ thị đó cho phạm vi kèo đã xác định, mỗi ticket dùng entry riêng.
- Màn hình xem trước hiển thị ticket Min Entry được chọn và tỷ lệ hiệu lực; lúc thực thi luôn xác định lại từ trạng thái hiện tại.

## Hủy nhánh chờ và từ khóa

- Khi bật chính sách, hủy **Max Entry còn chờ của cùng signal** ngay khi Min Entry có lần chốt thành công đầu tiên: SL, TP/TP1, chốt một phần, đóng tay hoặc đóng từ Telegram. Không cần đợi đóng hết Min Entry. Max Entry đã khớp thành vị thế thì không bị đóng bởi quy tắc này.
- Liên kết theo mã vị thế gốc và order mở từ broker; không suy đoán từ số trong comment. Lịch sử gom các deal chốt cùng vị thế, giữ từng lần chốt trong chi tiết. Deal chưa có liên kết hoặc không đọc được vị thế hiện tại sẽ hiện trạng thái chưa xác định.
- Tác vụ đối soát cần thêm bằng chứng deal thoát trong lịch sử 7 ngày; việc không thấy ticket trong danh sách không đủ để kết luận đã đóng.
- Hủy lệnh thất bại không được đưa vào danh sách “đã hủy”, nên có thể thử lại.
- Lệnh chủ động “đóng kèo” qua reply vẫn hủy lệnh chờ thuộc kèo theo yêu cầu rõ ràng của người dùng. Tắt tự hủy nhánh không vô hiệu hóa yêu cầu chủ động này hoặc `/kill`.
- “Đóng buy” chỉ đóng vị thế BUY; “đóng sell” chỉ đóng SELL trong phạm vi đã xác định.
- Từ khóa mơ hồ như “chốt nha”, “chốt lời”, “dừng” mặc định không được tự đóng lệnh. Dùng “chốt hết”, “đóng lệnh”, reply đúng kèo hoặc chọn ticket. Từ “dừng” cũng đã bỏ khỏi bộ mặc định mới.
- Tin vừa yêu cầu đóng hết vừa chốt một phần, hoặc có nhiều đích SL khác nhau, được đưa vào loại cần làm rõ.
- Tác vụ seed không tự bật lại rule mà người dùng đã tắt; dữ liệu rule riêng được giữ lại.

## Cài đặt

Mở `/config` và chọn mục cần đổi. Các thay đổi đều có xem trước/xác nhận; lựa chọn policy có nút riêng.

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `breakeven_mode` | `ticket` (cố định) | Tham số tương thích cấu hình cũ; không còn lựa chọn `setup` |
| `breakeven_offset_price` | `0` | Bù giá BUY cộng, SELL trừ |
| `breakeven_only_improve` | `true` | Giữ SL tự động nếu hiện tại bảo vệ tốt hơn |
| `tp1_enabled` | `true` | Tự xử lý TP1 khi có TP2 |
| `default_partial_close_percent` | `50` | Tỷ lệ chốt chung, TP1 và nút chốt |
| `generic_partial_uses_default` | `true` | Câu chốt bớt chung dùng tỷ lệ cấu hình |
| `auto_breakeven_on_partial` | `true` | Tự dời SL sau khi chốt, áp dụng cả TP1 |
| `tp1_min_volume_action` | `breakeven` | `breakeven` hoặc `skip` |
| `auto_cancel_pending_on_full_close` | `true` | Hủy Max Entry còn chờ khi Min Entry có lần chốt (giữ tên tham số cũ để tương thích) |
| `allow_multiple_partial_closes` | `false` | Cho phép chốt thêm sau lần đầu |
| `require_explicit_close` | `true` | Chặn từ khóa đóng lệnh mơ hồ |

Ví dụ command (cũng tạo bản xem trước):

```text
/config set default_partial_close_percent 30
/config set auto_breakeven_on_partial false
/config set tp1_min_volume_action skip
/config set auto_cancel_pending_on_full_close false
```

Cấu hình cũ `breakeven_mode=setup` được chuyển sang `ticket` khi seed/nạp; không thể bật lại chế độ trung bình qua cài đặt.

Các tham số mới được seed bổ sung vào nhóm cấu hình hiện có, không cần đổi schema. Màn hình xem trước tín hiệu hiển thị chính sách và tỷ lệ hiệu lực. Đọc cấu hình thất bại/không hợp lệ sẽ giữ bot ở trạng thái dừng mở mới.

## Xác minh và kích hoạt

183 kiểm thử tự động đã đạt với broker/DB giả lập; không đặt lệnh thật. Chưa kiểm thử đầu cuối với MT5/Telegram thật, chưa khởi động lại tiến trình bot. Thay đổi có hiệu lực khi chạy phiên bản mã mới; nên xác minh vòng đời kèo trên demo trước khi dùng với tài khoản thật.
