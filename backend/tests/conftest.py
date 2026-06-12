"""Backend test fixtures: import paths for app/ and the tenancy package."""
import os

# app/auth_jwt.py fail-loud отбивает плейсхолдерный SECRET_KEY на импорте;
# тесты не сорсят .env — даём тестовый секрет до любых импортов app.*
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))          # import app.*
sys.path.insert(0, str(BACKEND.parent))   # import tenancy.*

import pytest


class FakeRedis:
    """Минимальный async-стаб под команды, используемые auth_sessions."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):
        return self.store.get(key)

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def delete(self, key):
        self.store.pop(key, None)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def scan_iter(self, match=None):
        import fnmatch
        for k in list(self.store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


@pytest.fixture
def fake_redis():
    from app.redis_client import set_redis_for_tests

    client = FakeRedis()
    set_redis_for_tests(client)
    yield client
    set_redis_for_tests(None)
