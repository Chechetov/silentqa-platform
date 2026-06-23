"""Tenant dashboard users (email+password accounts with roles).

Login endpoints arrive in Phase-1 Plan 2; the table and the seed CLI need the
schema now. Password hashes are argon2 (argon2-cffi), NOT the bcrypt/passlib
stack used by brokers.

Revision ID: 012
Revises: 011
"""
from typing import Sequence, Union

from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role VARCHAR(10) NOT NULL CHECK (role IN ('admin', 'viewer')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login TIMESTAMPTZ
        )"""
    )
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (LOWER(email))")


def downgrade() -> None:
    op.execute("DROP TABLE users")
