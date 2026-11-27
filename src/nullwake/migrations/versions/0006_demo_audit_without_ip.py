"""Demo sign-ins are anonymous: drop the IP address stored on past auth.demo audit entries

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE audit_log SET detail = detail - 'ip' WHERE action = 'auth.demo' AND detail ? 'ip'")


def downgrade() -> None:
    pass  # the removed addresses are gone on purpose
