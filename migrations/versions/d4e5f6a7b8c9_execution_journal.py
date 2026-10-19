"""Durable broker journal and unique broker execution IDs.

Existing duplicate IDs intentionally fail migration and require reviewed reconciliation.
"""
from alembic import op

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_mt5_ticket ON trades (mt5_ticket)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_mt5_ticket ON orders (mt5_ticket)')
    op.execute('ALTER TABLE signal_receipts ADD COLUMN IF NOT EXISTS raw_message TEXT')
    op.execute('ALTER TABLE signal_receipts ADD COLUMN IF NOT EXISTS context JSONB')
    op.execute('ALTER TABLE trades ADD COLUMN IF NOT EXISTS mt5_position_id BIGINT')
    op.execute('ALTER TABLE trades ADD COLUMN IF NOT EXISTS mt5_order_ticket BIGINT')
    op.execute('CREATE INDEX IF NOT EXISTS ix_trades_mt5_position_id ON trades (mt5_position_id)')
    # init_db may already have created this table on deployments using automatic schema setup.
    from app.models.trade import BrokerCommand
    BrokerCommand.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    op.drop_index('ix_trades_mt5_position_id', table_name='trades')
    op.drop_column('trades', 'mt5_order_ticket')
    op.drop_column('trades', 'mt5_position_id')
    op.drop_column('signal_receipts', 'context')
    op.drop_column('signal_receipts', 'raw_message')
    op.drop_table('broker_commands')
    op.execute('ALTER TABLE trades DROP CONSTRAINT IF EXISTS uq_trades_mt5_ticket')
    op.execute('DROP INDEX IF EXISTS uq_trades_mt5_ticket')
    op.execute('ALTER TABLE orders DROP CONSTRAINT IF EXISTS uq_orders_mt5_ticket')
    op.execute('DROP INDEX IF EXISTS uq_orders_mt5_ticket')
