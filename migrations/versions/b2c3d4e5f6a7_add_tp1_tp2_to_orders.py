"""Add tp1 and tp2 columns to orders and take_profit_2 to parsed_signals

Revision ID: b2c3d4e5f6a7
Revises: 6fc62769571a
Create Date: 2026-10-14 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = '6fc62769571a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('tp1', sa.Numeric(precision=15, scale=5), nullable=True))
    op.add_column('orders', sa.Column('tp2', sa.Numeric(precision=15, scale=5), nullable=True))
    op.add_column('parsed_signals', sa.Column('take_profit_2', sa.Numeric(precision=15, scale=5), nullable=True))


def downgrade() -> None:
    op.drop_column('parsed_signals', 'take_profit_2')
    op.drop_column('orders', 'tp2')
    op.drop_column('orders', 'tp1')
