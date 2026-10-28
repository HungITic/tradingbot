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

## Trượt giá và limit chưa khớp

- Trượt **bất lợi lớn hơn** `max_slippage_entry`: tự đặt BUY_LIMIT/SELL_LIMIT tại Min Entry của signal, không chờ xác nhận trượt giá. Nếu có nhánh Max Entry thì vẫn đặt limit ở Max Entry như thường lệ. Trong ngưỡng hoặc trượt có lợi thì giữ hành vi market hiện có; giá có lợi vượt cả vùng entry vẫn có cơ chế duyệt riêng.
- Tổng lot, SL/TP sau bù 0.5 và cách chia TP1/TP2 giữ nguyên. Một limit nhánh có thể gồm hai lệnh chờ MT5. Giới hạn số lệnh, lỗ ngày, trùng tín hiệu và các chặn khác vẫn được kiểm tra/duyệt như trước.
- Khi có **action được parser nhận diện** liên quan đến signal đã tạo limit do trượt giá (chốt bớt, chốt entry xấu, đóng lệnh, dời SL, hòa vốn, vào lại), bot hủy các limit đó còn chờ. Quan hệ dùng reply trực tiếp/chuỗi reply trong đúng chat; action không reply dùng signal mở lệnh gần nhất trước action, giống phạm vi quản lý vị thế. Không hủy limit của kèo cũ khác hoặc vì một tin chào/cập nhật không được nhận diện là action. Phần đã khớp giữ nguyên và vẫn được xử lý theo action bình thường.
- Chính sách limit do trượt được lưu vào metadata signal trước khi đặt lệnh. Ý định hủy được commit vào risk audit trước khi gửi MT5; sync kiểm tra lại mỗi vòng và khôi phục việc hủy sau restart. Chỉ hủy ticket còn nằm trong pending snapshot, không dùng lệnh đóng vị thế thay thế khi limit đã khớp. Hủy thất bại được thử lại; kết quả không rõ thì dừng mở mới và giữ command journal để đối soát.
- Áp dụng cho lệnh mới có dấu chính sách này, không tự suy đoán hoặc hủy các limit cũ. Dùng JSON metadata/risk audit sẵn có nên không cần migration SQL. `/replay` vẫn xem trước/xác nhận, hiển thị việc sẽ hủy limit; không hủy khi mới xem trước.

## TP và chốt một phần

- **Lệnh mới có TP1 và TP2, tài khoản hedging:** mỗi nhánh entry được chia theo `default_partial_close_percent` lúc mở. Ví dụ 1.5 lot và 30% → 0.45 lot gắn TP1, 1.05 lot gắn TP2, cùng SL. Cả nhánh market và nhánh pending đều dùng TP trực tiếp phía broker. Hai phần là hai vị thế MT5 nhưng được gom thành một nhánh trong phần vị thế và lịch sử Telegram.
- Bot xác nhận `margin_mode=2` từ MT5. Tỷ lệ 0% hoặc tắt TP1 → chỉ mở phần TP2; 100% → chỉ mở phần TP1. Hai phần phải hợp lệ theo lot tối thiểu/bước lot của broker; không đủ lot thì từ chối mở nhánh, không âm thầm đổi tỷ lệ. Lot phần TP1 làm tròn xuống, phần TP2 giữ số dư để không tăng tổng lot.
- Tỷ lệ được cố định trong khối lượng vị thế lúc mở. Đổi cấu hình không chia lại vị thế đang mở. Giới hạn số vị thế/lệnh chờ tính cả các phần TP: hai nhánh entry, mỗi nhánh hai TP tương ứng tối đa bốn lệnh MT5.
- Hai phần được gửi tuần tự, nên giá khớp market có thể khác nhau. Giao diện nhánh hiển thị entry bình quân theo lot; khi dời hòa vốn, mỗi phần dùng entry thực tế riêng của nó.
- TP chạy phía broker khi bot offline. Sau khi xác nhận deal thoát phần TP1 có lý do TP, bot dời SL phần TP2 về entry riêng nếu bật `auto_breakeven_on_partial`. Dời SL vẫn cần bot hoạt động/kết nối; thất bại sẽ được thử lại. Không suy ra TP1 chỉ từ việc vị thế biến mất hoặc giá đã vượt TP1. Hoàn tất dời SL được lưu để khôi phục sau restart.
- **Lệnh cũ chưa tách:** tiếp tục dùng cơ chế kiểm tra giá TP1 và chốt một phần trước đây; không tự đóng/mở lại các vị thế cũ. Chính sách `tp1_min_volume_action` chỉ dùng cho cơ chế cũ.
- Chỉ có một TP: đóng toàn bộ tại TP đó. Bot không đồng thời chạy chốt một phần để cạnh tranh với TP trên MT5.
- Tỷ lệ chốt nhận từ 0 đến 100%: 0 bỏ qua chốt (với TP1 cũng không tự dời SL), 100 yêu cầu đóng hết khối lượng trong phạm vi đã chọn và không tự dời SL sau đó. Mức 100 vẫn được thực hiện khi đã chốt một phần trước đó.
- Với tỷ lệ lớn hơn 0 và nhỏ hơn 100, chốt một phần dùng `volume_min` và `volume_step` của broker, làm tròn xuống. Không đủ khối lượng hoặc phần dư nhỏ hơn lot tối thiểu thì không tự đóng hết vị thế.
- Khi TP1 thiếu lot, có hai lựa chọn: bỏ qua hoặc chỉ dời SL nếu đã bật tự dời. Tắt tự dời SL thì TP1 cũng không tự dời.
- Chốt TP1 thất bại: không dời SL, không đánh dấu đã xử lý thành công.
- Chốt thành công nhưng dời SL thất bại: không chốt lại; chỉ thử lại bước dời SL ở lần kiểm tra đủ điều kiện kế tiếp.
- Lịch sử hành động lưu ticket trong JSON để tra cứu kể cả khi chưa có bản ghi Position. Khi khởi động lại, bot kiểm tra lịch sử và khối lượng còn lại để tránh chốt lặp.
- `allow_multiple_partial_closes` áp dụng chung cho chốt thủ công, menu và tín hiệu; TP1 tự động luôn xử lý một lần. Khi bật chốt nhiều lần, người dùng có thể chủ động chốt thêm.
- Các câu chung như “chốt bớt” mặc định dùng tỷ lệ cấu hình chung, kể cả rule DB cũ còn mang giá trị 50%. Tỷ lệ ghi rõ trong tin nhắn luôn được ưu tiên. Rule riêng như “chốt một phần ba” vẫn giữ tỷ lệ riêng. Có thể tắt kế thừa tỷ lệ chung bằng `generic_partial_uses_default=false`.

## Chốt bớt entry xấu → chỉ Min Entry

- Tin có cụm **“chốt bớt” và từ “xấu”** được nhận diện là chốt một phần **Min Entry**. Ví dụ: “chốt bớt entry xấu”, “chốt bớt lệnh xấu”, “entry xấu thì chốt bớt”. Từ `XAU` (symbol vàng) không bị hiểu thành “xấu”.
- Dùng tỷ lệ riêng `bad_entry_partial_close_percent`, không phụ thuộc tỷ lệ chốt chung/TP1 (`default_partial_close_percent`). Nếu tin ghi rõ `30%` thì dùng 30%. Mức 0 bỏ qua chốt, mức 100 đóng hết Min Entry; Max Entry đã khớp không bị chốt.
- Ví dụ: Min Entry còn 0.10 lot, Max Entry còn 0.10 lot, tỷ lệ cấu hình 40% → chỉ chốt 0.04 lot của Min Entry, để lại 0.06 lot Min và 0.10 lot Max, tùy bước lot broker.
- Nếu Min Entry đã tách TP1/TP2, phần trăm chốt tính trên tổng khối lượng còn lại của nhánh một lần, không làm tròn độc lập từng vị thế con. Bot ưu tiên giảm phần TP1 rồi phần TP2, giữ đúng tổng lot cần đóng; không chia được theo bước lot thì từ chối. Mức 100% đóng tất cả vị thế con của Min Entry. Chặn chốt lặp vẫn áp dụng sau khi một phần đã đóng, kể cả sau restart; bật `allow_multiple_partial_closes` để chốt tiếp theo tỷ lệ dưới 100%.
- Chọn nhánh bằng nhãn đã lưu lúc đặt lệnh: `bot-min-entry`/`bot-override-min`; dùng nhãn broker khi thiếu nhãn DB. Không suy đoán Min bằng giá thấp/cao hoặc SL hiện tại. Nhãn DB và broker mâu thuẫn thì không tự chọn.
- Reply giới hạn vào đúng kèo. Nếu không có symbol/reply, chỉ xử lý khi có duy nhất một kèo Min Entry xác định. Có nhiều kèo phù hợp thì yêu cầu reply hoặc chỉ rõ “tất cả”.
- Không có Min Entry đang mở, nhãn không đủ rõ hoặc lot không đủ chia: không chuyển sang chốt Max Entry hoặc đóng hết lệnh tối thiểu.
- Nếu bật tự dời SL sau chốt, chỉ Min Entry được tự dời về entry riêng của nó. Tin có thêm chỉ thị “dời SL” riêng vẫn áp dụng chỉ thị đó cho phạm vi kèo đã xác định, mỗi ticket dùng entry riêng.
- Màn hình xem trước hiển thị ticket Min Entry được chọn và tỷ lệ hiệu lực; lúc thực thi luôn xác định lại từ trạng thái hiện tại.

## Hủy nhánh chờ và từ khóa

- Tin action quản lý vị thế quên reply được giới hạn vào **signal NEW_ORDER gần nhất trước tin action trong cùng chat**, theo message ID Telegram (gồm cả lần vào lại đã tạo NEW_ORDER). Không tìm signal mới hơn khi replay tin cũ, không chọn toàn bộ vị thế cùng symbol. Nếu có symbol trong action nhưng khác symbol của signal gần nhất thì từ chối và yêu cầu reply đúng kèo. Signal gần nhất chưa có order hoặc đã đóng hết → không chuyển sang kèo cũ còn mở.
- Reply rõ ràng được ưu tiên, tra đúng chat/message. Chỉ thị có “tất cả”/“toàn bộ”/“all” vẫn giữ phạm vi chủ động đó; lệnh Telegram theo ticket không bị đổi. “Vòng 2” mở mới vẫn dùng cơ chế reply-chain riêng. Replay action không reply hiển thị link kèo gần nhất trong xem trước và ghim signal ID khi xác nhận; lúc thực thi đọc lại ticket thuộc đúng signal, lưu phạm vi vào risk audit trước khi gửi MT5. Không cần schema mới.
- “Vòng 2”/“vào lại” có thể reply qua nhiều tin trung gian. Bot lần đúng chuỗi reply trong cùng chat đến signal mở lệnh hợp lệ gần nhất, tối đa 100 bước và 30 giây. Chỉ tin yêu cầu vòng mới được thực thi, dùng ID/thời gian của chính nó; không xử lý lại các tin trung gian. Áp dụng chung cho listener tự động và `/replay`; replay vẫn có xem trước/xác nhận/duyệt vượt giá như trước.
- Tin bị xóa, chuỗi đứt/lặp, reply khác chat, không tìm được signal hoặc signal có mức giá không hợp lệ → báo lý do và không gửi MT5. Provenance gồm chat, signal gốc, chuỗi message ID, nội dung gốc và lệnh vòng mới được lưu trong receipt context và metadata parsed signal (không cần schema mới). Sau restart có thể đọc lại liên kết đúng chat/message; bot vẫn đọc và đối chiếu nội dung signal gốc trên Telegram trước khi dùng liên kết đã lưu. Xem trước replay hiển thị link signal gốc và số bước reply.
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

`/replay` (và nạp link Telegram qua bot cá nhân) chỉ bỏ kiểm tra tín hiệu đã hết hạn theo `signal_expire_seconds`. Giữ nguyên xem trước, xác nhận, duyệt vượt giá và các kiểm tra giao dịch; thời gian gốc vẫn được lưu/hiển thị. Các kiểm tra thời gian gốc có múi giờ, thời gian ở tương lai và độ mới của giá thị trường vẫn áp dụng. Tín hiệu tự động vẫn kiểm tra hết hạn như trước.

Mở `/config` và chọn mục cần đổi. Các thay đổi đều có xem trước/xác nhận; lựa chọn policy có nút riêng.

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `breakeven_mode` | `ticket` (cố định) | Tham số tương thích cấu hình cũ; không còn lựa chọn `setup` |
| `breakeven_offset_price` | `0` | Bù giá BUY cộng, SELL trừ |
| `breakeven_only_improve` | `true` | Giữ SL tự động nếu hiện tại bảo vệ tốt hơn |
| `tp1_enabled` | `true` | Tự xử lý TP1 khi có TP2 |
| `default_partial_close_percent` | `50` | Tỷ lệ chốt chung, TP1 và nút chốt |
| `bad_entry_partial_close_percent` | `50` | Tỷ lệ riêng cho “chốt bớt” + “xấu”, tính trên khối lượng Min Entry còn lại; từ 0 đến 100 |
| `generic_partial_uses_default` | `true` | Câu chốt bớt chung dùng tỷ lệ cấu hình |
| `auto_breakeven_on_partial` | `true` | Tự dời SL sau khi chốt, áp dụng cả TP1 |
| `tp1_min_volume_action` | `breakeven` | `breakeven` hoặc `skip` |
| `auto_cancel_pending_on_full_close` | `true` | Hủy Max Entry còn chờ khi Min Entry có lần chốt (giữ tên tham số cũ để tương thích) |
| `allow_multiple_partial_closes` | `false` | Cho phép chốt thêm sau lần đầu |
| `require_explicit_close` | `true` | Chặn từ khóa đóng lệnh mơ hồ |

Ví dụ command (cũng tạo bản xem trước):

```text
/config set default_partial_close_percent 30
/config set bad_entry_partial_close_percent 40
/config set auto_breakeven_on_partial false
/config set tp1_min_volume_action skip
/config set auto_cancel_pending_on_full_close false
```

Cấu hình cũ `breakeven_mode=setup` được chuyển sang `ticket` khi seed/nạp; không thể bật lại chế độ trung bình qua cài đặt.

Các tham số mới được seed bổ sung vào nhóm cấu hình hiện có, không cần đổi schema. Khi nâng cấp, tỷ lệ entry xấu chưa tồn tại được khởi tạo bằng tỷ lệ chốt chung đang lưu để giữ hành vi hiện tại; sau đó chỉnh độc lập. Màn hình xem trước tín hiệu hiển thị chính sách và tỷ lệ hiệu lực. Đọc cấu hình thất bại/không hợp lệ sẽ giữ bot ở trạng thái dừng mở mới.

Liên kết phần TP dùng signal FK và nhãn nhánh/TP trong `orders.comment`, được commit cùng kết quả broker bởi command journal. Không cần migration schema mới. Trước khi gửi hai phần, cấu hình mở mới trong DB được tạm khóa; chỉ khôi phục sau khi cả hai phần đã xác nhận. Nếu tiến trình chết giữa hai lần gửi, restart giữ trạng thái dừng mở mới để đối soát. Nếu khớp thiếu, bị từ chối hoặc không rõ kết quả, bot không tự gửi bù/đóng ngược để giả lập rollback. Hai lệnh MT5 không thể là một giao dịch nguyên tử với PostgreSQL; các phần đã khớp vẫn giữ SL/TP riêng.

## Xác minh và kích hoạt

183 kiểm thử tự động đã đạt với broker/DB giả lập; không đặt lệnh thật. Chưa kiểm thử đầu cuối với MT5/Telegram thật, chưa khởi động lại tiến trình bot. Thay đổi có hiệu lực khi chạy phiên bản mã mới; nên xác minh vòng đời kèo trên demo trước khi dùng với tài khoản thật.
