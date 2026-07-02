"""Fairness-семафор: N слотов тенанта в Redis, compare-del release."""
import tasks.tenant_slots as ts


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def test_acquire_up_to_limit_then_none():
    r = FakeRedis()
    s1 = ts.try_acquire("acme", client=r, max_slots=2)
    s2 = ts.try_acquire("acme", client=r, max_slots=2)
    assert s1 is not None and s2 is not None
    assert s1[0] != s2[0]                       # разные ключи-слоты
    assert ts.try_acquire("acme", client=r, max_slots=2) is None


def test_slots_are_per_tenant():
    r = FakeRedis()
    assert ts.try_acquire("acme", client=r, max_slots=1) is not None
    assert ts.try_acquire("globex", client=r, max_slots=1) is not None  # чужой лимит не мешает


def test_release_frees_slot():
    r = FakeRedis()
    key, token = ts.try_acquire("acme", client=r, max_slots=1)
    assert ts.try_acquire("acme", client=r, max_slots=1) is None
    ts.release(key, token, client=r)
    assert ts.try_acquire("acme", client=r, max_slots=1) is not None


def test_release_wrong_token_is_noop():
    r = FakeRedis()
    key, token = ts.try_acquire("acme", client=r, max_slots=1)
    ts.release(key, "чужой-токен", client=r)
    assert ts.try_acquire("acme", client=r, max_slots=1) is None  # слот всё ещё занят
