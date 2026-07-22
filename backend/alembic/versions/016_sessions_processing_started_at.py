"""Add sessions.processing_started_at — момент фактического старта стадии 1 в воркере.

Метка нужна watchdog-правилам 3a/3b, чтобы отличать реально зависшую обработку от
легитимного ожидания fairness-слота и reprocess старых звонков (у которых
`created_at` древний, но обработка только что поставлена). Бэкенд сбрасывает метку
в NULL при постановке в `processing`; воркер проставляет NOW() при фактическом старте.

Revision ID: 016
Revises: 015
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "processing_started_at")
