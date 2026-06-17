import json

import pytest
from starlette.testclient import TestClient

ROWS = [{
    "slug": "acme", "schema_name": "t_acme", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": True,
    "modules": {"complexes": False},
}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def test_features_resolves_defaults(client):
    r = client.get("/api/tenancy/features")
    assert r.status_code == 200
    body = r.json()
    assert body["knowledge_base"] is True   # default ON
    assert body["complexes"] is False        # explicit OFF
    assert body["amocrm"] is False           # default OFF


def test_features_includes_display_name(monkeypatch, fake_redis):
    # реестр-строка с display_name → features отдаёт его наружу
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "display_name": "Клиника Фулдент"}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from starlette.testclient import TestClient
    c = TestClient(app, base_url="https://fulldent.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert r.json().get("display_name") == "Клиника Фулдент"


def test_features_includes_tenant_scenarios(monkeypatch, fake_redis, tmp_path):
    # company_config_id тенанта → файл со сценариями → features.scenarios (id+name)
    (tmp_path / "dental.json").write_text(json.dumps({
        "id": "dental",
        "scenarios": [{"id": "consultation", "name": "Консультация"}, {"id": "treatment_plan"}],
    }), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs._scenarios_for_config.cache_clear()
    cs._scenario_list_for_config.cache_clear()

    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "display_name": "Фулдент", "company_config_id": "dental"}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    c = TestClient(app, base_url="https://fulldent.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert r.json().get("scenarios") == [
        {"id": "consultation", "name": "Консультация"},
        {"id": "treatment_plan", "name": "treatment_plan"},
    ]


def test_features_no_scenarios_when_no_config(monkeypatch, fake_redis, tmp_path):
    # тенант без company_config_id (или с неизвестным) → ключ scenarios отсутствует
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs._scenarios_for_config.cache_clear()
    cs._scenario_list_for_config.cache_clear()

    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "company_config_id": None}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    c = TestClient(app, base_url="https://acme.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert "scenarios" not in r.json()
