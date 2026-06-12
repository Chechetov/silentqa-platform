"""platform_admins.totp_secret — задел под TOTP (спека §2).

Revision ID: s003
Revises: s002
"""
from typing import Sequence, Union

from alembic import op

revision: str = "s003"
down_revision: Union[str, None] = "s002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE shared.platform_admins ADD COLUMN totp_secret TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE shared.platform_admins DROP COLUMN totp_secret")
