from app import provision_tenant


def test_seed_kb_category_find_or_create():
    executed = []

    class FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return None  # not present → insert path

    class FakeConn:
        def cursor(self):
            return FakeCur()

    provision_tenant._seed_kb_category(FakeConn(), "t_acme")
    joined = "\n".join(executed)
    assert "kb_categories" in joined
    assert "ON CONFLICT (slug) DO NOTHING" in joined
    assert "Термины" in str(executed) or "terms" in joined.lower()
