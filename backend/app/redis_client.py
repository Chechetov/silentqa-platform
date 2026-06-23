"""Lazy async Redis client shared by dashboard sessions and rate limiting.

Connection is created on first use (redis.asyncio connects lazily), so
importing this module never performs I/O. Tests inject a fake via
set_redis_for_tests().
"""
from __future__ import annotations

import redis.asyncio as redis

from .config import settings

_client = None


def get_redis():
    global _client
    if _client is None:
        _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def set_redis_for_tests(client) -> None:
    """Test seam: replace (or with None — reset) the singleton."""
    global _client
    _client = client
