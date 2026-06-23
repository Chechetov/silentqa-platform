"""B3 — миграция 008 сидит Zoom-ЖК шаблон ТОЛЬКО в схемы тенантов с complexes.

Существующие схемы застемплены за 008 (не перезапустится); правка влияет лишь на
будущие провижининги. Pure SQL-shape тест (как test_migration_014_kb)."""
import importlib.util
import unittest.mock as mock
from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "008_seed_zoom_meeting_template.py"


def _load():
    spec = importlib.util.spec_from_file_location("m008", MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chain_unchanged():
    m = _load()
    assert m.revision == "008" and m.down_revision == "007"


def test_upgrade_seeds_only_complexes_tenants():
    m = _load()
    with mock.patch.object(m.op, "execute") as ex:
        m.upgrade()
    sql = "\n".join(c.args[0] for c in ex.call_args_list)
    assert "INSERT INTO extraction_templates" in sql
    assert "Zoom-встреча брокера (презентация ЖК)" in sql
    # гейт: сидим только если у схемы текущего тенанта complexes=true
    assert "WHERE EXISTS" in sql
    assert "shared.tenants" in sql
    assert "current_schema()" in sql
    assert "modules->>'complexes'" in sql
    assert "ON CONFLICT (name) DO NOTHING" in sql


def test_upgrade_gate_polarity_seeds_when_complexes_true():
    """Пинит ПОЛЯРНОСТЬ гейта: сидим при complexes = true (а не = false), через
    EXISTS (а не NOT EXISTS). Substring-shape тест иначе пропустил бы инверсию,
    которая засеяла бы RE-шаблон ровно в не-complexes тенантов."""
    m = _load()
    with mock.patch.object(m.op, "execute") as ex:
        m.upgrade()
    sql = "\n".join(c.args[0] for c in ex.call_args_list)
    assert "COALESCE((modules->>'complexes')::boolean, false) = true" in sql
    assert "NOT EXISTS" not in sql


def test_downgrade_unchanged():
    m = _load()
    with mock.patch.object(m.op, "execute") as ex:
        m.downgrade()
    sql = "\n".join(c.args[0] for c in ex.call_args_list)
    assert "DELETE FROM extraction_templates WHERE name = 'Zoom-встреча брокера (презентация ЖК)'" in sql
