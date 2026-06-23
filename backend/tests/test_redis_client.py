"""redis_client: lazy singleton + test seam."""
from app.redis_client import get_redis, set_redis_for_tests


class _Stub:
    pass


def test_set_redis_for_tests_overrides_singleton():
    stub = _Stub()
    set_redis_for_tests(stub)
    assert get_redis() is stub
    set_redis_for_tests(None)


def test_get_redis_returns_client_with_decoded_responses():
    set_redis_for_tests(None)
    client = get_redis()
    # redis.asyncio.Redis: соединение ленивое, сам объект создаётся без I/O
    assert client.connection_pool.connection_kwargs.get("decode_responses") is True
    set_redis_for_tests(None)
