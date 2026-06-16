import importlib.util
from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "alembic_shared" / "versions" / "s004_tenant_modules.py"


def _load():
    spec = importlib.util.spec_from_file_location("s004", MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    m = _load()
    assert m.revision == "s004"
    assert m.down_revision == "s003"


def test_upgrade_adds_column_and_backfills_by_rule():
    m = _load()
    sql = "\n".join(c.args[0] for c in _calls(m))
    assert "ADD COLUMN IF NOT EXISTS modules jsonb" in sql
    # realestate gets complexes+amocrm on; everyone keeps knowledge_base implicit-on
    assert "slug = 'realestate'" in sql
    assert '"complexes": true' in sql and '"amocrm": true' in sql


def _calls(m):
    import unittest.mock as mock
    with mock.patch.object(m.op, "execute") as ex:
        m.upgrade()
    return ex.call_args_list
