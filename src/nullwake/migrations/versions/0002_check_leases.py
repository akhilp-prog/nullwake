"""check leases: a run is claimed with an expiring lease instead of by advancing next_run_at

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('monitors', sa.Column('lease_owner', sa.String(length=120), nullable=True))
    op.add_column('monitors', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('monitors', 'lease_expires_at')
    op.drop_column('monitors', 'lease_owner')
