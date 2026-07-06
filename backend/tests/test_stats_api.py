import asyncio
import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(fake_redis, role="viewer", employee=None, email="v@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", email, role,
                                                      employee_name=employee)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


KPI = {"calls": 10, "scored_calls": 8, "avg_score": 6.5,
       "risk_calls": 2, "avg_talk_ratio": 0.61}


@pytest.fixture
def stub_stats(monkeypatch):
    from app.routes import stats as st
    captured = {}

    async def kpi(db, scope, start, end, scenario=None):
        captured.setdefault("kpi_scopes", []).append(scope)
        return dict(KPI)

    async def series(db, scope, start, end, granularity, scenario=None):
        captured["granularity"] = granularity
        return [{"bucket": "2026-07-01", "calls": 3, "avg_score": 7.0}]

    monkeypatch.setattr(st, "_kpi_row", kpi)
    monkeypatch.setattr(st, "_series_rows", series)
    return captured


def test_overview_shape_and_prev(client, fake_redis, stub_stats):
    r = client.get("/api/stats/overview?days=30", cookies=_cookie(fake_redis))
    assert r.status_code == 200
    body = r.json()
    assert body["kpi"] == KPI and body["prev_kpi"] == KPI
    assert body["series"][0]["bucket"] == "2026-07-01"
    assert len(stub_stats["kpi_scopes"]) == 2  # текущее + предыдущее окно


def test_overview_granularity_validation(client, fake_redis, stub_stats):
    assert client.get("/api/stats/overview?granularity=week",
                      cookies=_cookie(fake_redis)).status_code == 200
    assert client.get("/api/stats/overview?granularity=hour",
                      cookies=_cookie(fake_redis)).status_code == 422


def test_manager_scope_passed_to_sql(client, fake_redis, stub_stats):
    client.get("/api/stats/overview",
               cookies=_cookie(fake_redis, role="manager", employee="Иванов"))
    assert stub_stats["kpi_scopes"][0] == "Иванов"


def test_unauthenticated_401(client, fake_redis):
    assert client.get("/api/stats/overview").status_code == 401


def test_managers_endpoint_shape(client, fake_redis, monkeypatch):
    from app.routes import stats as st

    async def rows(db, scope, start, scenario=None):
        return [{"name": "Иванов", "calls": 5, "avg_score": 7.2,
                 "risk_calls": 1, "avg_talk_ratio": 0.55,
                 "last_call_date": "2026-07-01T00:00:00+00:00"}]

    async def spark(db, scope, start, scenario=None):
        return [{"name": "Иванов", "bucket": "2026-06-29", "avg_score": 7.0}]

    monkeypatch.setattr(st, "_manager_rows", rows)
    monkeypatch.setattr(st, "_spark_rows", spark)
    r = client.get("/api/stats/managers", cookies=_cookie(fake_redis))
    assert r.status_code == 200
    m = r.json()[0]
    assert m["name"] == "Иванов" and m["spark"] == [7.0]


def test_objections_and_risk_calls(client, fake_redis, monkeypatch):
    from app.routes import stats as st

    async def obj(db, scope, start, scenario=None):
        return [{"category": "too_expensive", "count": 4, "resolved_rate": 0.5,
                 "avg_handling_quality": 6.0, "examples": ["Дорого"]}]

    async def risk(db, scope, start, limit, scenario=None):
        return [{"session_id": "s1", "employee": None, "score": 2,
                 "risk_flags": ["low_score"], "created_at": "2026-07-01T00:00:00+00:00"}]

    monkeypatch.setattr(st, "_objection_rows", obj)
    monkeypatch.setattr(st, "_risk_rows", risk)
    assert client.get("/api/stats/objections",
                      cookies=_cookie(fake_redis)).json()[0]["category"] == "too_expensive"
    assert client.get("/api/stats/risk-calls",
                      cookies=_cookie(fake_redis)).json()[0]["risk_flags"] == ["low_score"]


def test_scenario_sql_in_all_helpers():
    import inspect
    from app.routes import stats as st
    src = inspect.getsource(st)
    # каждый из 6 хелперов использует единый _SCENARIO_SQL в WHERE
    assert src.count("{_SCENARIO_SQL}") >= 6


def test_scenario_id_propagated_to_helpers(client, fake_redis, monkeypatch):
    """scenario_id из каждого из 4 роутов долетает до соответствующих хелперов."""
    from app.routes import stats as st
    captured: dict[str, str | None] = {}

    def _cap(name, shape):
        async def _fn(*a, **k):
            captured[name] = k.get("scenario")
            return shape
        return _fn

    monkeypatch.setattr(st, "_kpi_row", _cap("kpi", dict(KPI)))
    monkeypatch.setattr(st, "_series_rows", _cap("series", []))
    monkeypatch.setattr(st, "_manager_rows", _cap("managers", []))
    monkeypatch.setattr(st, "_spark_rows", _cap("spark", []))
    monkeypatch.setattr(st, "_objection_rows", _cap("objections", []))
    monkeypatch.setattr(st, "_risk_rows", _cap("risk", []))

    cookies = _cookie(fake_redis)
    assert client.get("/api/stats/overview?scenario_id=general",
                      cookies=cookies).status_code == 200
    assert captured["kpi"] == "general"      # прокинут и в kpi, и в prev_kpi
    assert captured["series"] == "general"

    assert client.get("/api/stats/managers?scenario_id=general",
                      cookies=cookies).status_code == 200
    assert captured["managers"] == "general"
    assert captured["spark"] == "general"

    assert client.get("/api/stats/objections?scenario_id=general",
                      cookies=cookies).status_code == 200
    assert captured["objections"] == "general"

    assert client.get("/api/stats/risk-calls?scenario_id=general",
                      cookies=cookies).status_code == 200
    assert captured["risk"] == "general"
