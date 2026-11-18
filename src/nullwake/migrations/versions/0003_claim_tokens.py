"""claim tokens (fencing) and a run-request counter for "Run now"

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('monitors', sa.Column('lease_token', sa.String(length=36), nullable=True))
    op.add_column('monitors', sa.Column('run_requests', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('monitors', 'run_requests')
    op.drop_column('monitors', 'lease_token')
