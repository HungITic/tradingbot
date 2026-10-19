# Rà soát ACID — 19/10/2026

## Kết luận và phạm vi

Đã rà các điểm ghi dữ liệu/thực thi trong `app/`, migrations và script bảo trì: nhận signal, duyệt lệnh, mở hai nhánh, SL/TP, chốt một phần, hủy lệnh chờ, cấu hình/rule, đồng bộ lịch sử và shutdown. Parser, normalizer, risk calculation và trình bày không tự ghi giao dịch; các quyết định thực thi đi qua coordinator.

**Không thể gọi toàn bộ PostgreSQL + MT5 + Telegram là một transaction ACID.** MT5 không tham gia transaction của PostgreSQL; rollback DB không hoàn tác một giao dịch đã khớp. Hai nhánh entry cũng không có cam kết cùng khớp hoặc cùng hoàn tác. Bản sửa bảo vệ transaction DB và ghi nhật ký trước mọi yêu cầu thay đổi MT5; trường hợp không xác định dừng gửi để đối soát, không tự thực thi lại.

## Kiểm tra môi trường thật

Đã SSH tới máy được chỉ định, đọc trạng thái container `trading-bot-db`:

- PostgreSQL 16.15, container healthy, dữ liệu gắn Docker volume tại `/var/lib/postgresql/data`.
- `fsync=on`, `synchronous_commit=on`, `full_page_writes=on`; isolation mặc định `read committed`.
- Không tìm thấy mã deal/order trùng trong dữ liệu hiện có, nhưng chỉ có unique primary key của row, chưa có unique index cho mã MT5.
- 48 row `trades.position_id IS NULL`. Không suy đoán hoặc tự gắn chúng vào một vị thế khác.

Không chạy migration, gửi lệnh MT5, sửa cấu hình hay sửa dữ liệu trong `trading_bot`. Kiểm thử ghi dữ liệu dùng DB `codex_acid_20261019_01` riêng; DB này đã được xóa sau khi kiểm thử.

## Các điểm đã sửa

| Thuộc tính | Lỗ hổng | Bản sửa |
|---|---|---|
| Atomicity | Có thể báo thành công dù commit order/management/rule lỗi | Order/management lỗi được đẩy lên; dừng mở thêm. Rule chỉ thay trong memory sau commit và trả thất bại khi DB lỗi |
| Atomicity | Task bị hủy không đi qua nhánh rollback rõ ràng | Transaction scope rollback cả `CancelledError` |
| Atomicity | MT5 đã nhận nhưng DB mất kết quả | Commit `BrokerCommand.SUBMITTED` trước gửi; commit response và bản ghi order mở trong cùng transaction sau xác nhận |
| Consistency | `SELECT` rồi `INSERT` deal có thể đua nhau | Unique index trên ticket order/deal + `INSERT ... ON CONFLICT` |
| Consistency | Lịch sử mất mã vị thế/entry, đoán từ comment | Giữ dữ liệu gốc từ bridge, lưu thêm mã vị thế/order broker trong ledger, chỉ ghép bằng định danh broker |
| Consistency | Nhận signal có claim nhưng mất nội dung nếu crash | Lưu raw message và context cùng transaction với claim |
| Consistency | Cấu hình chưa commit đã hiện trong memory | Chỉ công bố candidate sau khi lưu thành công; cache order cũng cập nhật sau commit |
| Isolation | Hai process có thể cùng chạy coordinator | PostgreSQL application lease; process thứ hai bị từ chối. Trước gửi kiểm tra lease còn tồn tại |
| Isolation | Các command MT5 có thể chen nhau | Advisory transaction lock theo account cho các command; chỉ một luồng I/O dùng bridge tại một thời điểm |
| Isolation | Cancel task có thể nhả luồng khi thread MT5 vẫn chạy | Đợi worker kết thúc trước khi nhả I/O lock; shutdown đợi sync task và execution lane |
| Isolation | Dừng khẩn cấp đua với cập nhật config | Reassert và lưu pause trong execution lane sau transaction config đang chạy |
| Durability | Crash sau gửi có thể dẫn tới gửi lặp | `SUBMITTED`/`UNKNOWN` tồn tại trong DB và chặn gửi thêm sau restart |
| Durability | Cursor sync tiến dù batch ghi lỗi | Chỉ tiến cursor sau khi xử lý batch thành công |
| Bypass | Script bridge có thể gửi lệnh ngoài coordinator | Script kiểm tra bridge chuyển thành chỉ đọc, không gửi lệnh demo trực tiếp |

Migration `d4e5f6a7b8c9` thêm journal, raw receipt, định danh ledger và unique indexes. Nếu dữ liệu có duplicate, migration **thất bại và rollback**, không tự xóa/ghép bản ghi tài chính. `init_db` cũng tạo các phần schema này cho luồng startup hiện có.

## Kiểm thử

11 kiểm thử đã chạy thành công trên PostgreSQL thật, DB riêng:

- Rollback khi exception và khi task bị hủy.
- 15 lần nhập cùng deal đồng thời chỉ có một row thắng.
- Lưu broker acknowledgement và order trong cùng transaction.
- Commit intent lỗi thì không gửi MT5.
- Timeout để lại `UNKNOWN`, tiếp tục chặn gửi sau khi dispose/reconnect pool.
- Lỗi DB sau broker acknowledgement để lại `SUBMITTED`; transaction response/order rollback, không gửi lại.
- Các command qua nhiều connection PostgreSQL được tuần tự hóa.
- 10 lần claim cùng message chỉ một lần thắng; raw message/context còn nguyên.
- Process thứ hai không lấy được application lease; mất lease thì không gửi MT5.
- Migration từ schema cũ giữ nguyên row tài chính mẫu.

Các lệnh MT5 trong kiểm thử được giả lập. Không thử trên tài khoản thật, không tắt PostgreSQL thật hoặc giả lập mất điện máy chủ.

Chạy lại với một database tạm do người vận hành tạo:

```sh
ACID_TEST_DB_HOST=<postgres-host> ACID_TEST_DB_NAME=codex_acid_<unique-name> DEBUG=false .venv/bin/python -m pytest tests/test_acid -q
```

Fixture từ chối tên DB không bắt đầu bằng `codex_acid_`; fixture tạo/xóa bảng trong DB kiểm thử. Credentials dùng cấu hình DB hiện tại, không in ra log. Không trỏ tới database đang sử dụng.

## Giới hạn và việc triển khai còn lại

1. Bản sửa chưa triển khai/chưa restart bot. Cần áp dụng migration, chạy bot từ entrypoint có application lease; một DB phục vụ một tài khoản MT5 cấu hình. Các writer ngoài bot/terminal không chịu khóa của PostgreSQL.
2. 48 liên kết lịch sử thiếu vẫn cần backfill từ lịch sử broker theo deal ticket chính xác. Sync mới bổ sung mã broker cho những deal đã đọc lại, không tự đoán các row ngoài cửa sổ lịch sử. Chưa xác nhận các row cũ đã được sửa.
3. `SUBMITTED`/`UNKNOWN` cần được đối chiếu với lịch sử MT5 rồi xử lý có kiểm soát. Không xóa journal hoặc đổi sang `REJECTED` chỉ vì không thấy vị thế đang mở; lệnh có thể đã khớp và đóng. Chưa có quy trình tự phục hồi an toàn cho kết quả không có định danh xác nhận.
4. Telegram vẫn là thông báo best effort; chưa có transactional outbox bảo đảm phát lại thông báo khi mạng lỗi. Mất thông báo không được dùng để kết luận lệnh chưa thực thi.
5. Các kiểm thử chứng minh những ranh giới đã nêu, không chứng nhận mọi tổ hợp lỗi, mất điện hoặc phục hồi backup. Chưa kiểm tra backup/restore hay failover của máy chủ.

## Tham chiếu thiết kế

- [PostgreSQL: explicit/advisory locking](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS).
- [PostgreSQL: INSERT ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html).
- [SQLAlchemy: asyncio session và transaction](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).
