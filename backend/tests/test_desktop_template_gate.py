"""Авто-Zoom-шаблон применяется ТОЛЬКО на AmoCRM-тенантах (realestate), не на fulldent.

Инвариант: `source:desktop-app` сессия на fulldent НЕ получает `template_id`;
та же на realestate — получает (резолвер шаблона запатчен).

Механика auth: используем строку реестра с `api_key_required=False`
(санкционированная планом альтернатива), поэтому X-API-Key не нужен —
ingestion проходит как «tokenless rollout». Так тест не зависит от внутреннего
имени функции сверки ключа. `get_db` переопределён фейковой async-сессией,
ловящей добавленный Session.
"""
import uuid

import pytest
from starlette.testclient import TestClient

from app.models import SessionStatus


class _FakeResult:
    def __init__(self, val):
        self._val = val

    def scalar(self):
        return self._val

    def first(self):
        return None


class _FakeDB:
    """Минимальная async-сессия: ловит добавленный Session, отдаёт chunks_count=0.

    `refresh` дозаполняет server-side/Python-side дефолты, которые на не-флашнутом
    ORM-инстансе ещё не проставлены (id, created_at, status) — иначе `_to_response`
    упадёт на required-поле `status` SessionResponse.
    """

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        import datetime as _dt

        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _dt.datetime(2026, 6, 17, tzinfo=_dt.timezone.utc)
        if getattr(obj, "status", None) is None:
            obj.status = SessionStatus.created

    async def scalar(self, *a, **k):
        return 0

    async def execute(self, *a, **k):
        return _FakeResult(0)


def _client(monkeypatch, fake_redis, slug, host):
    """TestClient с реестром-строкой тенанта (api_key_required=False) и фейковым get_db."""
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": slug, "schema_name": f"t_{slug}", "status": "active",
             "custom_domains": [], "api_key_hash": None,
             "api_key_required": False, "modules": {}}]

    async def fake_all(self):
        return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)

    from app.main import app
    from app.database import get_db
    fake = _FakeDB()

    async def _get_db_override():
        yield fake
    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app, base_url=host), fake


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def test_fulldent_desktop_session_no_zoom_template(monkeypatch, fake_redis):
    # AMOCRM_TENANT_SLUGS не содержит fulldent → шаблон НЕ ставится.
    # Резолвер шаблона патчим «взрывом»: на fulldent он не должен даже вызываться.
    from app.routes import sessions as sess_mod

    async def boom(db):
        raise AssertionError("_resolve_default_desktop_template_id не должен вызываться на не-AmoCRM тенанте")
    monkeypatch.setattr(sess_mod, "_resolve_default_desktop_template_id", boom)

    c, fake = _client(monkeypatch, fake_redis, "fulldent", "https://fulldent.silentqa.com")
    r = c.post("/api/sessions",
               json={"metadata": {"source": "desktop-app", "employee": "Доктор"}})
    assert r.status_code == 201, r.text
    sess = fake.added[-1]
    assert (sess.metadata_ or {}).get("template_id") is None
    assert (sess.metadata_ or {}).get("employee") == "Доктор"


def test_realestate_desktop_session_gets_zoom_template(monkeypatch, fake_redis):
    # realestate ∈ AMOCRM_TENANT_SLUGS → шаблон ставится (резолвер запатчен).
    from app.routes import sessions as sess_mod

    async def fake_resolve(db):
        return "tpl-zoom-id"
    monkeypatch.setattr(sess_mod, "_resolve_default_desktop_template_id", fake_resolve)

    c, fake = _client(monkeypatch, fake_redis, "realestate", "https://realestate.silentqa.com")
    r = c.post("/api/sessions",
               json={"metadata": {"source": "desktop-app"}})
    assert r.status_code == 201, r.text
    assert (fake.added[-1].metadata_ or {}).get("template_id") == "tpl-zoom-id"
