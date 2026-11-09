"""Persist new-signal admission history across restarts."""
from alembic import op

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    from app.models.entry_admission import EntryAdmission
    EntryAdmission.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    op.drop_table('entry_admissions')
