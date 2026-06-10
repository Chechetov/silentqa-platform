"""
Per-lead mutual exclusion via Redis.

Wraps pipeline stages so concurrent calls on the same lead_id serialise,
preventing races between prior_context reads and subsequent metadata
writes.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any

import redis

logger = logging.getLogger(__name__)

LOCK_KEY_PREFIX = "lead_lock:"
# Must exceed the Celery task hard limit (task_time_limit=5400s in
# celery_app.py). The lock is held around the entire heavy pipeline
# (transcription → diarization → 2 LLM calls → AmoCRM push), so a TTL shorter
# than the worst-case task duration would let the Redis key expire mid-pipeline
# and a concurrent task on the same lead would slip into the critical section —
# defeating the lock for exactly the long calls it matters for. Buffer above
# 5400s covers task teardown before the key is released.
DEFAULT_TIMEOUT_SEC = 5700

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

_default_client: redis.Redis | None = None


def _get_default_client() -> redis.Redis:
    global _default_client
    if _default_client is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/3")
        _default_client = redis.Redis.from_url(url)
    return _default_client


@contextmanager
def lead_lock(
    lead_id: int | None,
    *,
    client: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    poll_interval: float = 1.0,
):
    """Acquire a lock scoped to lead_id; no-op when lead_id is falsy."""
    if not lead_id:
        yield
        return

    redis_client = client or _get_default_client()
    key = f"{LOCK_KEY_PREFIX}{lead_id}"
    token = str(uuid.uuid4())

    acquired = redis_client.set(key, token, nx=True, ex=timeout)
    waited = 0.0
    while not acquired:
        time.sleep(poll_interval)
        waited += poll_interval
        if waited >= timeout:
            logger.warning(
                f"lead_lock: waited {waited:.0f}s for lead {lead_id}; "
                f"proceeding without lock (upstream lock may be stale)"
            )
            yield
            return
        acquired = redis_client.set(key, token, nx=True, ex=timeout)

    logger.debug(f"lead_lock: acquired lead {lead_id}")
    try:
        yield
    finally:
        try:
            redis_client.eval(_RELEASE_LUA, 1, key, token)
            logger.debug(f"lead_lock: released lead {lead_id}")
        except Exception:
            logger.exception(f"lead_lock: failed to release lead {lead_id}")
