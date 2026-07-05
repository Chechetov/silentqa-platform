"""Миграция 018: таблица quality_results — структурные гарантии по исходнику.

Тесты без БД: читаем файл миграции и проверяем DDL-инварианты,
на которые опираются worker (upsert) и backend (/api/stats).
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "alembic" / "versions" / "018_quality_results.py").read_text(encoding="utf-8")


def test_creates_table_with_key_columns():
    assert "CREATE TABLE quality_results" in SRC
    for col in ("session_id", "overall_score", "version", "scenario_id",
                "employee", "session_created_at", "duration_seconds",
                "skip_reason", "criteria", "objections", "talk_metrics",
                "sentiment_counts", "risk_flags", "updated_at"):
        assert col in SRC, f"нет колонки {col}"


def test_pk_fk_and_indexes():
    assert "PRIMARY KEY" in SRC
    assert "REFERENCES sessions(id) ON DELETE CASCADE" in SRC
    assert "ix_qr_created" in SRC
    assert "ix_qr_employee_created" in SRC


def test_downgrade_drops_table():
    assert "DROP TABLE IF EXISTS quality_results" in SRC
