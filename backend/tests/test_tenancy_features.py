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


def test_amocrm_subdomain_helper_reads_config(monkeypatch, tmp_path):
    # amocrm_subdomain берётся из company-config (домен-специфика → конфиг тенанта)
    (tmp_path / "re.json").write_text(json.dumps({
        "id": "re", "amocrm_subdomain": "rogovestate.amocrm.ru",
    }), encoding="utf-8")
    (tmp_path / "noamo.json").write_text(json.dumps({"id": "noamo"}), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs.clear_scenario_caches()
    assert cs.amocrm_subdomain_for("re") == "rogovestate.amocrm.ru"
    assert cs.amocrm_subdomain_for("noamo") is None
    assert cs.amocrm_subdomain_for(None) is None
    assert cs.amocrm_subdomain_for("missing") is None


def test_features_includes_amocrm_subdomain_when_module_on(monkeypatch, fake_redis, tmp_path):
    (tmp_path / "realestate.json").write_text(json.dumps({
        "id": "realestate", "amocrm_subdomain": "rogovestate.amocrm.ru",
    }), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs.clear_scenario_caches()

    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "realestate", "schema_name": "t_realestate", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {"amocrm": True}, "company_config_id": "realestate"}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    c = TestClient(app, base_url="https://realestate.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert r.json().get("amocrm_subdomain") == "rogovestate.amocrm.ru"


def test_features_omits_amocrm_subdomain_when_module_off(monkeypatch, fake_redis, tmp_path):
    # модуль amocrm выключен → субдомен не утекает наружу, даже если в конфиге есть
    (tmp_path / "x.json").write_text(json.dumps({
        "id": "x", "amocrm_subdomain": "foo.amocrm.ru",
    }), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs.clear_scenario_caches()

    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {"amocrm": False}, "company_config_id": "x"}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    c = TestClient(app, base_url="https://acme.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert "amocrm_subdomain" not in r.json()


def test_card_label_helper_reads_config(monkeypatch, tmp_path):
    # card-label берётся из company-config (card_extraction.label) — домен-специфика
    # → конфиг тенанта, не хардкод во фронте.
    (tmp_path / "chechetov.json").write_text(json.dumps({
        "id": "chechetov", "card_extraction": {"label": "Итоги созвона"},
    }), encoding="utf-8")
    (tmp_path / "nocard.json").write_text(json.dumps({"id": "nocard"}), encoding="utf-8")
    (tmp_path / "emptylabel.json").write_text(json.dumps({
        "id": "emptylabel", "card_extraction": {"label": "  "},
    }), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs.clear_scenario_caches()
    assert cs.card_label_for("chechetov") == "Итоги созвона"
    assert cs.card_label_for("nocard") is None
    assert cs.card_label_for("emptylabel") is None   # пустой label → None
    assert cs.card_label_for(None) is None
    assert cs.card_label_for("missing") is None


def test_features_includes_card_label(monkeypatch, fake_redis, tmp_path):
    # company_config_id тенанта → card_extraction.label → features.card_label
    (tmp_path / "chechetov.json").write_text(json.dumps({
        "id": "chechetov", "card_extraction": {"label": "Итоги созвона"},
    }), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs.clear_scenario_caches()

    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "chechetov", "schema_name": "t_chechetov", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "company_config_id": "chechetov"}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    c = TestClient(app, base_url="https://chechetov.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert r.json().get("card_label") == "Итоги созвона"


def test_features_omits_card_label_when_absent(monkeypatch, fake_redis, tmp_path):
    # конфиг без card_extraction → ключ card_label отсутствует (фронт упадёт на дефолт)
    (tmp_path / "nocard.json").write_text(json.dumps({"id": "nocard"}), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    import app.company_scenarios as cs
    cs.clear_scenario_caches()

    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "company_config_id": "nocard"}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    c = TestClient(app, base_url="https://acme.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert "card_label" not in r.json()


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
