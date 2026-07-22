"""Add sessions.enqueued_at — момент постановки сессии в обработку (enqueue-time якорь).

Watchdog-правило 3b раньше опиралось на `created_at`: reprocess старого звонка
(created_at древний) → бэкенд ставит processing + processing_started_at=NULL →
задача ждёт слот (concurrency=1 занят) → 3b видит древний created_at и ложно
помечает failed. Массовая переоценка по scenario_id выкашивала бы всю очередь
за один свип. `enqueued_at` фиксирует момент постановки в обработку, и 3b
матчит по нему (COALESCE-fallback на created_at — только для легаси-строк,
зависших в processing до этой миграции, у которых enqueued_at ещё NULL).

Revision ID: 017
Revises: 016
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "enqueued_at")
