"""Tests for download_recording's KZ-proxy-first / direct-fallback routing.

Context: RU recording hosts are intermittently unreachable from Hetzner, so
downloads go through a SOCKS5 tunnel egressing from the KZ relay. If the proxy
itself is down, we must still try a direct connection rather than fail hard.
A definitive 404 must NOT trigger fallback (the file is genuinely gone).
"""
from unittest.mock import MagicMock

import tasks.amocrm_sync as amo


class _Resp:
    def __init__(self, status=200, chunks=(b"audio",)):
        self.status_code = status
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        return iter(self._chunks)


def test_proxy_first_then_direct_fallback(monkeypatch, tmp_path):
    """Proxy attempt raises -> falls back to a direct connection and succeeds."""
    monkeypatch.setattr(amo, "RECORDING_PROXY", "socks5h://127.0.0.1:1080")
    seen = []

    def fake_get(url, stream=True, timeout=60, proxies=None):
        seen.append(proxies)
        if proxies is not None:
            raise ConnectionError("proxy down")
        return _Resp(200)

    monkeypatch.setattr(amo.requests, "get", fake_get)
    dest = str(tmp_path / "full.mp3")

    assert amo.download_recording("https://media.comagic.ru/x/y", dest) is True
    assert seen[0] is not None and seen[1] is None  # proxy tried first, then direct
    assert len(seen) == 2


def test_http_error_does_not_fall_back(monkeypatch, tmp_path):
    """An HTTP error response (404 expired, 400 'no files to concat') is
    definitive — the origin answered, so no direct fallback is attempted."""
    monkeypatch.setattr(amo, "RECORDING_PROXY", "socks5h://127.0.0.1:1080")
    for status in (404, 400):
        calls = []

        def fake_get(url, stream=True, timeout=60, proxies=None):
            calls.append(proxies)
            return _Resp(status)

        monkeypatch.setattr(amo.requests, "get", fake_get)

        assert amo.download_recording("https://media.comagic.ru/x/y", str(tmp_path / "f.mp3")) is False
        assert len(calls) == 1, f"status {status} should not fall back to direct"
