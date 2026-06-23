import unittest.mock as mock

from app import provision_tenant


def test_provision_insert_includes_modules():
    captured = {}

    class FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "INSERT INTO shared.tenants" in sql:
                captured["sql"] = sql
                captured["params"] = params

        def fetchone(self):
            return None

    class FakeConn:
        autocommit = False

        def cursor(self):
            return FakeCur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    with mock.patch.object(provision_tenant, "shared_connect", return_value=FakeConn()), \
         mock.patch.object(provision_tenant, "run_tenant", create=True), \
         mock.patch("app.migrate.run_tenant"), \
         mock.patch.object(provision_tenant, "_seed_admin"), \
         mock.patch.object(provision_tenant, "_set_api_key", return_value="sqa_x"), \
         mock.patch.object(provision_tenant, "_seed_kb_category", create=True):
        provision_tenant.provision("acme", "ACME", "a@b.io", "pw")

    assert "modules" in captured["sql"]
    assert '"knowledge_base": true' in captured["params"][-1]
    assert '"complexes": false' in captured["params"][-1]
