"""Evaluation profiles: per-tenant, per-scenario versioned criteria + eval prompt.

Каждая строка — версия профиля оценки для сценария (criteria + prompt). Ровно
одна версия активна на сценарий (partial unique index). Архив версий не удаляется
автоматически — откат = активировать старую версию.

Revision ID: 015
Revises: 014
"""
from typing import Sequence, Union

from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE evaluation_profile_versions (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scenario_id  text NOT NULL,
            version      integer NOT NULL,
            criteria     jsonb NOT NULL DEFAULT '[]'::jsonb,
            prompt       text,
            source       text NOT NULL DEFAULT 'manual',
            wishes_input text,
            rewrite_meta jsonb,
            is_active    boolean NOT NULL DEFAULT false,
            created_by   uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            UNIQUE (scenario_id, version)
        )
    """)
    # Ровно одна активная версия на сценарий.
    op.execute("""
        CREATE UNIQUE INDEX ux_eval_profile_active
        ON evaluation_profile_versions (scenario_id)
        WHERE is_active
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS evaluation_profile_versions")
