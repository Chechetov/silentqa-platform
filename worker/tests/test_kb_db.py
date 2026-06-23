import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as DbSession

from tasks import knowledge_base as kb

requires_db = pytest.mark.skipif(
    "DATABASE_URL_SYNC" not in os.environ, reason="integration test needs DATABASE_URL_SYNC"
)


def test_kb_record_mentions_degrades_without_tables(monkeypatch):
    # tenant_connect raises (no schema/ctx) → wrapper must swallow and not raise
    def boom():
        raise RuntimeError("no tenant ctx")

    monkeypatch.setattr(kb, "tenant_connect", boom, raising=False)
    kb.kb_record_mentions(uuid.uuid4(), [("e1", 2)])  # must NOT raise


@requires_db
def test_record_mentions_upsert_replaces_count():
    # uses search_path of DATABASE_URL_SYNC; assumes alembic head (014) applied.
    url = os.environ["DATABASE_URL_SYNC"]
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        with conn.begin() as trans:
            with DbSession(bind=conn, expire_on_commit=False) as s:
                cat = uuid.uuid4()
                ent = uuid.uuid4()
                sid = uuid.uuid4()
                s.execute(text("INSERT INTO kb_categories (id,name,slug,is_taxonomy) VALUES (:i,'C',:sl,true)"),
                          {"i": cat, "sl": "c-" + cat.hex[:6]})
                s.execute(text("INSERT INTO kb_entries (id,category_id,term) VALUES (:i,:c,'T')"),
                          {"i": ent, "c": cat})
                s.execute(text("INSERT INTO sessions (id,status) VALUES (:i,'completed')"), {"i": sid})
                s.execute(text("""INSERT INTO kb_entry_mentions (entry_id,session_id,count) VALUES (:e,:s,5)
                                  ON CONFLICT (entry_id,session_id) DO UPDATE SET count=EXCLUDED.count"""),
                          {"e": ent, "s": sid})
                s.execute(text("""INSERT INTO kb_entry_mentions (entry_id,session_id,count) VALUES (:e,:s,2)
                                  ON CONFLICT (entry_id,session_id) DO UPDATE SET count=EXCLUDED.count"""),
                          {"e": ent, "s": sid})
                row = s.execute(text("SELECT count FROM kb_entry_mentions WHERE entry_id=:e AND session_id=:s"),
                                {"e": ent, "s": sid}).first()
                assert row[0] == 2  # replace, not +=
            trans.rollback()
