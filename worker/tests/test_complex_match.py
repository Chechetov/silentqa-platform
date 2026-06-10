"""Integration tests for match_or_create_complex against a real PostgreSQL DB.

Requires DATABASE_URL_SYNC pointing to the dev DB. Tests create their own data
inside a transaction that's rolled back at the end.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as DbSession

from tasks.complex_match import match_or_create_complex


@pytest.fixture
def db():
    url = os.environ["DATABASE_URL_SYNC"]
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        with conn.begin() as trans:
            with DbSession(bind=conn, expire_on_commit=False) as s:
                yield s
            trans.rollback()


@pytest.fixture
def template_id(db):
    row = db.execute(text("SELECT id FROM extraction_templates WHERE name='Презентация ЖК'")).first()
    assert row, "seed template missing — run alembic upgrade head first"
    return row[0]


@pytest.fixture
def session_id(db):
    sid = uuid.uuid4()
    db.execute(text("INSERT INTO sessions (id, status) VALUES (:id, 'completed')"), {"id": sid})
    return sid


def _insert_extraction(db, session_id, template_id, raw):
    eid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO complex_extractions (id, session_id, template_id, raw_data)
        VALUES (:id, :sid, :tid, CAST(:raw AS jsonb))
    """), {"id": eid, "sid": session_id, "tid": template_id, "raw": json.dumps(raw)})
    return eid


def test_creates_new_complex_when_none_exists(db, template_id, session_id):
    eid = _insert_extraction(db, session_id, template_id, {
        "name": "Шагал", "developer": "Эталон", "class": "бизнес", "additional_info": []
    })
    cid = match_or_create_complex(db, eid)
    assert cid is not None
    row = db.execute(text("SELECT name, name_normalized, developer FROM complexes WHERE id=:id"), {"id": cid}).first()
    assert row.name == "Шагал"
    assert row.name_normalized == "шагал"
    assert row.developer == "Эталон"


def test_matches_existing_by_normalized_name(db, template_id, session_id):
    e1 = _insert_extraction(db, session_id, template_id,
                            {"name": "ЖК «Шагал»", "developer": "Эталон", "additional_info": []})
    cid1 = match_or_create_complex(db, e1)

    sid2 = uuid.uuid4()
    db.execute(text("INSERT INTO sessions (id, status) VALUES (:id, 'completed')"), {"id": sid2})
    e2 = _insert_extraction(db, sid2, template_id,
                            {"name": "Шагал", "developer": "ГК Эталон", "additional_info": ["скидка"]})
    cid2 = match_or_create_complex(db, e2)

    assert cid1 == cid2  # matched, not created twice
    count = db.execute(text("SELECT COUNT(*) FROM complexes WHERE name_normalized='шагал'")).scalar()
    assert count == 1


def test_aggregate_is_recomputed_on_match(db, template_id, session_id):
    e1 = _insert_extraction(db, session_id, template_id,
                            {"name": "Шагал", "developer": "Эталон",
                             "location": {"parks": ["Парк А"]}, "additional_info": []})
    match_or_create_complex(db, e1)

    sid2 = uuid.uuid4()
    db.execute(text("INSERT INTO sessions (id, status) VALUES (:id, 'completed')"), {"id": sid2})
    e2 = _insert_extraction(db, sid2, template_id,
                            {"name": "Шагал", "developer": "Эталон",
                             "location": {"parks": ["Парк Б"]}, "class": "бизнес", "additional_info": []})
    cid = match_or_create_complex(db, e2)

    row = db.execute(text("SELECT aggregated_data, class FROM complexes WHERE id=:id"), {"id": cid}).first()
    parks = row.aggregated_data["location"]["parks"]
    assert set(parks) == {"Парк А", "Парк Б"}
    assert row[1] == "бизнес"  # class column populated from latest extraction


def test_skips_when_name_or_developer_missing(db, template_id, session_id):
    e1 = _insert_extraction(db, session_id, template_id,
                            {"name": None, "developer": "Эталон", "additional_info": []})
    cid = match_or_create_complex(db, e1)
    assert cid is None
