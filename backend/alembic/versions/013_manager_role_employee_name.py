"""Роль manager + привязка employee_name + индекс под агрегаты (спека §5, §8).

Revision ID: 013
Revises: 012
"""
from typing import Sequence, Union

from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CHECK создан инлайном в 012 → автоимя users_role_check
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_role_check "
        "CHECK (role IN ('admin', 'viewer', 'manager'))"
    )
    op.execute("ALTER TABLE users ADD COLUMN employee_name TEXT")
    op.execute(
        "CREATE INDEX ix_sessions_employee_created "
        "ON sessions ((metadata->>'employee'), created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_sessions_employee_created")
    op.execute("ALTER TABLE users DROP COLUMN employee_name")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_check")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_role_check "
        "CHECK (role IN ('admin', 'viewer'))"
    )
