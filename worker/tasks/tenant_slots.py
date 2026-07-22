"""Per-tenant fairness: лимит одновременных тяжёлых задач на тенанта.

N именованных слотов в Redis (SET NX EX). Задача без слота уходит в
self.retry с countdown — слот ВОРКЕРА освобождается для задач других
тенантов, а сама задача вернётся позже. TTL страхует утечку слота при
жёстком падении воркера (та же логика, что у lead_lock).
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import redis

logger = logging.getLogger(__name__)

# TTL > task_time_limit (5400s в celery_app.py) — иначе слот истечёт под
# живой длинной задачей и лимит перестанет работать ровно там, где нужен.
DEFAULT_TTL_SEC = int(os.getenv("TENANT_SLOT_TTL_SEC", "5700"))
DEFAULT_MAX_SLOTS = int(os.getenv("TENANT_MAX_CONCURRENT", "2"))

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

_default_client: redis.Redis | None = None


def _get_default_client() -> redis.Redis:
    global _default_client
    if _default_client is None:
        # Дефолт = дефолт брокера (celery_app.py:13), НЕ легаси lead_lock (6379/3)
        url = os.getenv("REDIS_URL", "redis://localhost:6381/0")
        _default_client = redis.Redis.from_url(url)
    return _default_client


def try_acquire(slug: str, *, client: Any | None = None,
                max_slots: int | None = None,
                ttl: int = DEFAULT_TTL_SEC) -> tuple[str, str] | None:
    """Занять слот тенанта. None — все слоты заняты (вызывающий должен retry)."""
    redis_client = client or _get_default_client()
    n = max_slots if max_slots is not None else DEFAULT_MAX_SLOTS
    token = str(uuid.uuid4())
    for i in range(n):
        key = f"tslot:{slug}:{i}"
        if redis_client.set(key, token, nx=True, ex=ttl):
            logger.debug(f"tenant slot acquired: {key}")
            return key, token
    return None


def release(key: str, token: str, *, client: Any | None = None) -> None:
    """Освободить слот (compare-del: чужой токен не трогаем)."""
    redis_client = client or _get_default_client()
    try:
        redis_client.eval(_RELEASE_LUA, 1, key, token)
    except Exception:
        logger.exception(f"tenant slot release failed: {key}")
