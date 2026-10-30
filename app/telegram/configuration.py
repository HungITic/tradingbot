"""Shared catalog of editable runtime settings in the Telegram console."""

CONFIG_FIELDS = {
    "enabled": "Cho phép mở lệnh mới", "require_sl": "Bắt buộc có SL",
    "require_tp": "Bắt buộc có TP", "enable_dual_entry": "Vào hai đầu vùng giá",
    "max_slippage_entry": "Dung sai giá", "lot_min_entry": "Lot nhánh thứ nhất (Min Entry)",
    "lot_max_entry": "Lot nhánh thứ hai (Max Entry)", "max_open_positions": "Số lệnh tối đa (gồm lệnh chờ)",
    "min_lot": "Lot tối thiểu mỗi lệnh", "max_lot": "Lot tối đa mỗi lệnh",
    "allowed_symbols": "Mã được phép giao dịch",
    "duplicate_timeout_seconds": "Thời gian lọc trùng (giây)",
    "broker_utc_offset_hours": "Múi giờ sàn (UTC)",
    "default_lot": "Lot mặc định", "default_partial_close_percent": "Tỷ lệ chốt một phần (%)",
    "bad_entry_partial_close_percent": "Tỷ lệ chốt bớt entry xấu (%)",
    "auto_breakeven_on_partial": "Dời SL sau khi chốt một phần",
    "generic_partial_uses_default": "Chốt bớt chung dùng tỷ lệ cấu hình",
    "require_explicit_close": "Yêu cầu từ khóa đóng lệnh rõ ràng",
    "breakeven_only_improve": "Giữ SL đang bảo vệ tốt hơn",
    "breakeven_offset_price": "Bù giá hòa vốn (BUY cộng, SELL trừ)",
    "tp1_enabled": "Tự xử lý TP1 khi có TP2",
    "tp1_min_volume_action": "TP1 khi không đủ lot để chia",
    "auto_cancel_pending_on_full_close": "Hủy Max Entry khi Min Entry có lần chốt",
    "allow_multiple_partial_closes": "Cho phép chốt một phần nhiều lần",
    "repeat_partial_close_keywords": "Câu/từ khóa được phép chốt tiếp",
    "max_daily_loss_percent": "Giới hạn lỗ ngày (%)", "signal_expire_seconds": "Thời hạn tín hiệu (giây)",
}
CONFIG_SECTIONS = {
    "entry": ("🎯 Vào lệnh", {"enabled", "enable_dual_entry", "max_slippage_entry", "lot_min_entry", "lot_max_entry", "default_lot", "min_lot", "max_lot"}),
    "sl": ("🛡 SL và hòa vốn", {"breakeven_offset_price", "breakeven_only_improve", "auto_breakeven_on_partial"}),
    "profit": ("✂️ Chốt lời và nhánh lệnh", {"default_partial_close_percent", "bad_entry_partial_close_percent", "tp1_enabled", "tp1_min_volume_action", "auto_cancel_pending_on_full_close", "allow_multiple_partial_closes", "repeat_partial_close_keywords"}),
    "risk": ("⚖️ Giới hạn tài khoản", {"require_sl", "require_tp", "allowed_symbols", "max_open_positions", "max_daily_loss_percent"}),
    "signal": ("📥 Xử lý tín hiệu", {"generic_partial_uses_default", "require_explicit_close", "signal_expire_seconds", "duplicate_timeout_seconds"}),
    "broker": ("🕒 Giờ sàn", {"broker_utc_offset_hours"}),
}


CONFIG_HELP = {
    "allow_multiple_partial_closes": "Bật: mọi signal chốt một phần và lệnh chốt tay được chốt tiếp. Tắt: chỉ signal chứa câu/từ khóa trong danh sách ngoại lệ được chốt tiếp. Signal ngược chiều tự chốt 50% và đóng 100% giữ quy tắc riêng.",
    "repeat_partial_close_keywords": "Nhập các câu/cụm từ cách nhau bằng dấu phẩy hoặc xuống dòng. Ví dụ: chốt bớt entry xấu, chốt bớt 1 phần. Tin phải chứa nguyên cụm (không phân biệt hoa/thường) và được parser nhận diện là chốt một phần. Danh sách cho phép chốt tiếp dù trước đó đã chốt/TP1, khi chốt lặp chung tắt. Mỗi lần lưu thay thế toàn bộ danh sách; nhập [] để xóa. Không tự thêm từ khóa giao dịch mới cho parser; không thay đổi tỷ lệ, phạm vi hay cơ chế chống xử lý trùng.",
    "max_slippage_entry": "Mức trượt giá bất lợi tối đa để vào market. Vượt mức này: tự đặt BUY_LIMIT/SELL_LIMIT tại entry signal, không chờ duyệt trượt giá. Có action liên quan đến kèo thì hủy các limit đó còn chờ; không đóng phần đã khớp.",
    "default_partial_close_percent": "Tỷ lệ TP1 của lệnh mới được dùng để chia lot ngay khi mở; TP1/TP2 chạy trên MT5. Đổi tỷ lệ không tự chia lại lệnh đang mở. Tỷ lệ này cũng dùng cho chốt chung; entry xấu có tỷ lệ riêng.",
    "bad_entry_partial_close_percent": "Chỉ áp dụng cho tin có ‘chốt bớt’ và ‘xấu’: chốt phần trăm khối lượng còn lại của Min Entry. Nhập từ 0 đến 100: 0 bỏ qua chốt, 100 đóng hết Min Entry. Phần trăm ghi rõ trong tin được ưu tiên; TP1 và chốt chung dùng tỷ lệ riêng.",
    "min_lot": "Giới hạn dưới cho mỗi lệnh. Lot mặc định và cả hai nhánh phải nằm trong khoảng min/max.",
    "max_lot": "Giới hạn trên cho mỗi lệnh, không phải tổng lot của tài khoản. Lot mặc định và cả hai nhánh phải nằm trong khoảng min/max.",
    "lot_min_entry": "Khối lượng nhánh Min Entry (đầu giá thấp của vùng entry), không phải giới hạn lot tối thiểu.",
    "lot_max_entry": "Khối lượng nhánh Max Entry (đầu giá cao của vùng entry), không phải giới hạn lot tối đa.",
    "default_lot": "Khối lượng mặc định khi vào một lệnh. Phải nằm trong giới hạn lot tối thiểu/tối đa.",
    "allowed_symbols": "Nhập các mã cách nhau bằng dấu phẩy, gồm cả hậu tố sàn. Ví dụ: XAUUSD,XAUUSD.sc. Danh sách mới thay thế danh sách hiện tại.",
    "duplicate_timeout_seconds": "Khoảng thời gian chặn tín hiệu cùng mã và cùng chiều. Nhập số giây từ 0 trở lên.",
    "signal_expire_seconds": "Tín hiệu mở lệnh quá thời hạn này sẽ bị từ chối. Nhập số giây lớn hơn 0.",
    "broker_utc_offset_hours": "Độ lệch giờ máy chủ sàn so với UTC, từ -12 đến 14. Ví dụ UTC+3 nhập 3; UTC+2 nhập 2. Dùng để quy đổi thời gian giao dịch.",
}

CONFIG_GROUP_HELP = {
    "entry": "Lot tối thiểu/tối đa là giới hạn mỗi lệnh. Min Entry/Max Entry là hai đầu giá của vùng entry.\n\nNếu cần đổi nhiều lot cùng lúc, gửi ví dụ:\n<code>/config set min_lot=0.02 max_lot=0.10 default_lot=0.02 lot_min_entry=0.02 lot_max_entry=0.02</code>",
    "sl": "Hòa vốn dùng entry riêng từng lệnh. Bù giá BUY cộng, SELL trừ.",
    "profit": "Một TP: đóng toàn bộ.\nLệnh mới có TP1 + TP2: chia lot theo tỷ lệ, gắn TP trực tiếp trên MT5. Đổi tỷ lệ áp dụng cho lần mở sau; không đủ lot để chia sẽ từ chối mở.\nMuốn chọn riêng câu được chốt tiếp: tắt chốt nhiều lần chung, rồi nhập danh sách câu/từ khóa ngoại lệ.",
    "risk": "Giới hạn lệnh tính cả vị thế và lệnh chờ. Danh sách mã áp dụng cho lệnh mở mới.",
    "signal": "Thời hạn tín hiệu và thời gian lọc trùng được tính bằng giây.",
    "broker": "Giờ hiển thị dùng giờ Việt Nam. Mục này cấu hình giờ máy chủ sàn để quy đổi lịch sử giao dịch.",
}
