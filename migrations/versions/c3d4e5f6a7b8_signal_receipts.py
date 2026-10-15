"""Add durable Telegram message execution claims."""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("signal_receipts",
                    sa.Column("chat_id", sa.BigInteger(), primary_key=True),
                    sa.Column("message_id", sa.BigInteger(), primary_key=True),
                    sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))

    op.execute("""INSERT INTO signal_receipts (chat_id, message_id)
SELECT DISTINCT src.chat_id, sig.telegram_message_id
FROM signals sig JOIN signal_sources src ON src.id = sig.source_id
JOIN orders ord ON ord.signal_id = sig.id
WHERE sig.telegram_message_id IS NOT NULL AND ord.mt5_ticket IS NOT NULL
ON CONFLICT DO NOTHING""")


def downgrade():
    op.drop_table("signal_receipts")
