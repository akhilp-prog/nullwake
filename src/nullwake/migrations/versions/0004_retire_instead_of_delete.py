"""retire checks and contracts instead of deleting them, so incidents, runs and queued alerts survive

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('monitors', sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('contracts', sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('contracts', 'retired_at')
    op.drop_column('monitors', 'retired_at')
