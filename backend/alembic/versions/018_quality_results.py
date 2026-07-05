"""quality_results: денормализованная проекция отчётов качества для аналитики.

Файлы quality.json остаются источником истины для drill-down; эта таблица —
для SQL-агрегаций дашборда руководителя (тренды/лидерборд/возражения/риски).
Пишет worker (results_db.record_quality_result), читает backend (/api/stats).

Revision ID: 018
Revises: 017
"""
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE quality_results (
            session_id UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            overall_score SMALLINT,
            version SMALLINT,
            scenario_id TEXT,
            employee TEXT,
            session_created_at TIMESTAMPTZ NOT NULL,
            duration_seconds REAL,
            skip_reason TEXT,
            criteria JSONB,
            objections JSONB,
            talk_metrics JSONB,
            sentiment_counts JSONB,
            risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_qr_created ON quality_results (session_created_at)")
    op.execute("CREATE INDEX ix_qr_employee_created ON quality_results (employee, session_created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quality_results")
