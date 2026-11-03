"""Durable Telegram queue delivered outside the MT5 execution lane."""
from alembic import op

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    # Startup create_all may already have created it on existing installations.
    from app.models.notification import TelegramNotification
    TelegramNotification.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    op.drop_table('telegram_notifications')
