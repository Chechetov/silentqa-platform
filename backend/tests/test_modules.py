def test_registry_row_exposes_modules(monkeypatch):
    # async registry maps the modules column into the cached row dict
    import asyncio
    from app.tenancy_http import TenantRegistry

    class FakeRow:
        slug = "acme"; schema_name = "t_acme"; status = "active"
        custom_domains = []; api_key_hash = None; api_key_required = True
        modules = {"knowledge_base": True, "complexes": False}

    class FakeRes:
        def __iter__(self): return iter([FakeRow()])

    class FakeDB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return FakeRes()

    monkeypatch.setattr("app.database.async_session", lambda: FakeDB())
    reg = TenantRegistry(ttl_seconds=0)
    rows = asyncio.run(reg.all_tenants())
    assert rows[0]["modules"] == {"knowledge_base": True, "complexes": False}
