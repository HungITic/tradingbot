# Xử lý ngoại lệ signal lỗi giá

Áp dụng cho signal mở lệnh XAUUSD (gồm hậu tố sàn) bị từ chối do cấu trúc giá,
trước khi gửi yêu cầu MT5. Signal hợp lệ đi theo quy trình cũ. Không tự gửi lại
khi MT5 trả lỗi, mất kết nối hoặc kết quả gửi lệnh chưa xác định.

## Lỗi entry

Ví dụ `SELL 3956 – 3864`, SL `3973`, TP `3943 – 3930`, giá hiện tại gần `3956`:

- Hai entry cách nhau hơn ngưỡng cho phép khi xử lý lỗi.
- Giữ đúng một entry gốc gần giá hiện tại nhất: `3956`; không đoán entry mới.
- Entry được giữ phải phù hợp với SL/TP. Hai entry cách giá bằng nhau thì từ chối.
- Bỏ entry còn lại. Khi bật dual entry, dùng `lot_min_entry`; khi tắt, dùng
  `default_lot`. Không gộp lot của nhánh bị bỏ vào nhánh còn lại.
- Phần lot này vẫn chia TP1/TP2 theo tỷ lệ đang cài, nên trên MT5 có thể có hai
  vị thế cùng giá entry. Nhãn nhánh được giữ là Min Entry để các action theo
  signal, entry xấu và TP tiếp tục xác định đúng phạm vi.

## Lỗi SL/TP

Bot chỉ thông báo lỗi và dừng để người dùng gửi lại SL/TP đúng. Không đoán giá,
không tạo đề xuất hoặc nút duyệt sửa SL/TP, không tự vào lệnh với mức đã suy đoán.
Không tự thêm SL/TP bị thiếu. Nếu bỏ entry xa mà SL/TP vẫn không hợp lệ thì cũng
dừng và thông báo; fallback entry không được bỏ qua lỗi SL/TP.

## Cấu hình

Trong `/config`, nhóm **Xử lý tín hiệu**:

| Khóa | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `signal_recovery_enabled` | `true` | Bật xử lý ngoại lệ |
| `recovery_max_entry_gap` | `20` | Hai entry của signal đã lỗi cách nhau hơn ngưỡng này thì xét bỏ entry xa |
| `recovery_max_quote_distance` | `30` | Khoảng cách tối đa từ entry gần nhất tới giá hiện tại |
| `recovery_max_sl_distance` | `100` | Khoảng cách tối đa từ SL tới mọi entry được giữ |
| `recovery_max_tp_distance` | `200` | Khoảng cách tối đa từ mỗi TP tới mọi entry được giữ |

Các khoảng cách là đơn vị giá XAU, phải hữu hạn và lớn hơn 0. Đây là ngưỡng của
cơ chế xử lý signal đã lỗi, không sửa hoặc thu hẹp các mức của signal hợp lệ.

## Thực thi và nhật ký

Dùng báo giá mới (BUY lấy ask, SELL lấy bid), sau đó chạy lại kiểm tra hiện có:
thời hạn, giới hạn tài khoản, khối lượng/bước lot, trượt giá và chống xử lý trùng.
SL/TP gốc được bù 0.5 giá đúng một lần theo rule đã có. Trượt bất lợi vẫn đặt limit
theo rule hiện tại; các giới hạn cần duyệt vẫn yêu cầu duyệt.

`/replay` giữ xem trước và xác nhận. Bản xem trước ghi rõ phương án và lot.
Khi xác nhận, tính lại bằng giá hiện tại; nếu phương án hoặc lot thay đổi thì
yêu cầu xem trước lại. Việc bỏ kiểm tra hết hạn của replay không bỏ các kiểm
tra khác, kể cả báo giá cũ và thời gian nằm trong tương lai.

Raw message và các cột giá gốc được giữ nguyên. Quyết định, giá thực thi trước
offset, báo giá và lot fallback được lưu vào metadata `signal_recovery` và
RiskEvent trong cùng transaction, commit trước mọi thao tác thay đổi MT5.
Không ghi được quyết định thì dừng. Telegram thông báo rõ thay đổi và gắn link
signal như các thông báo hiện tại. Không cần migration schema mới.
