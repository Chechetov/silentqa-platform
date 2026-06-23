"""Unit tests for provision_tenant refactor (Task 8).

No DB connections required — tests only pure/logic pieces.
Integration with a real DB is covered in Task 19.
"""
from __future__ import annotations


def test_generate_api_key_format():
    from app.provision_tenant import generate_api_key

    key, digest = generate_api_key()
    assert key.startswith("sqa_") and len(key) > 20
    import hashlib

    assert hashlib.sha256(key.encode()).hexdigest() == digest


def test_provision_rejects_bad_slug():
    import pytest
    from app.provision_tenant import provision

    with pytest.raises(ValueError):
        provision("Bad Slug!", "x", "a@b.c", "pw")  # до любых коннектов
