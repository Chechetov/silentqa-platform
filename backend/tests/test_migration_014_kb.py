import importlib.util
import unittest.mock as mock
from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "014_knowledge_base.py"


def _load():
    spec = importlib.util.spec_from_file_location("m014", MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chain_and_tables():
    m = _load()
    assert m.revision == "014" and m.down_revision == "013"
    with mock.patch.object(m.op, "execute") as ex:
        m.upgrade()
    sql = "\n".join(c.args[0] for c in ex.call_args_list)
    assert "CREATE TABLE kb_categories" in sql
    assert "CREATE TABLE kb_entries" in sql
    assert "CREATE TABLE kb_entry_mentions" in sql
    assert "UNIQUE (slug)" in sql or "unique(slug)" in sql.lower()
    assert "UNIQUE (category_id, term)" in sql or "unique(category_id, term)" in sql.lower()
    assert "UNIQUE (entry_id, session_id)" in sql or "unique(entry_id, session_id)" in sql.lower()
    assert "REFERENCES sessions(id) ON DELETE CASCADE" in sql


def test_downgrade_drops_all():
    m = _load()
    with mock.patch.object(m.op, "execute") as ex:
        m.downgrade()
    sql = "\n".join(c.args[0] for c in ex.call_args_list)
    assert "DROP TABLE IF EXISTS kb_entry_mentions" in sql
    assert "DROP TABLE IF EXISTS kb_entries" in sql
    assert "DROP TABLE IF EXISTS kb_categories" in sql
