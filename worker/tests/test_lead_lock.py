"""Tests for the per-lead Redis lock context manager."""
import threading
import time
from unittest.mock import MagicMock

import pytest

from tasks.lead_lock import lead_lock, LOCK_KEY_PREFIX, DEFAULT_TIMEOUT_SEC


def test_ttl_outlives_celery_hard_limit():
    """The lock TTL must exceed the Celery task hard limit, or the Redis key can
    expire mid-pipeline and let a concurrent same-lead task into the critical
    section. Pins the invariant against future changes to either constant."""
    from tasks.celery_app import app

    hard_limit = app.conf.task_time_limit
    assert hard_limit is not None
    assert DEFAULT_TIMEOUT_SEC > hard_limit


def test_no_lock_when_lead_id_falsy():
    """With no lead_id, lock is a no-op — yields immediately."""
    client = MagicMock()
    with lead_lock(0, client=client):
        pass
    with lead_lock(None, client=client):
        pass
    client.set.assert_not_called()


def test_acquires_on_first_try():
    client = MagicMock()
    client.set.return_value = True
    with lead_lock(42, client=client, poll_interval=0.01):
        pass
    args, kwargs = client.set.call_args_list[0]
    assert args[0] == f"{LOCK_KEY_PREFIX}42"
    assert kwargs.get("nx") is True
    assert kwargs.get("ex") == 5700


def test_releases_only_own_token():
    client = MagicMock()
    client.set.return_value = True
    with lead_lock(42, client=client, poll_interval=0.01):
        pass
    assert client.eval.called
    eval_args = client.eval.call_args[0]
    assert eval_args[2] == f"{LOCK_KEY_PREFIX}42"
    set_token = client.set.call_args[0][1]
    eval_token = eval_args[3]
    assert set_token == eval_token


def test_waits_and_retries_until_acquired():
    client = MagicMock()
    client.set.side_effect = [False, False, True]
    start = time.time()
    with lead_lock(42, client=client, poll_interval=0.01):
        pass
    elapsed = time.time() - start
    assert client.set.call_count == 3
    assert elapsed >= 0.02


def test_custom_timeout_respected():
    client = MagicMock()
    client.set.return_value = True
    with lead_lock(42, client=client, timeout=1200, poll_interval=0.01):
        pass
    assert client.set.call_args[1]["ex"] == 1200


def test_timeout_escape_proceeds_without_lock():
    """If acquisition never succeeds within timeout, yield without lock and skip release."""
    client = MagicMock()
    client.set.return_value = False  # never acquires
    entered = False
    # 0.05s timeout, 0.02s poll → escape after ~3 attempts
    with lead_lock(42, client=client, timeout=0.05, poll_interval=0.02):
        entered = True
    assert entered is True
    # Release (eval) must NOT be called — we never held the lock
    assert not client.eval.called
