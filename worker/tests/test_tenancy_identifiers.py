"""Tests for tenancy.identifiers — slug/schema validation and derivation."""
import pytest

from tenancy.identifiers import (
    RESERVED_SLUGS,
    schema_for_slug,
    slug_from_schema,
    validate_schema_name,
    validate_slug,
)


def test_valid_slug_roundtrip():
    assert validate_slug("realestate") == "realestate"
    assert schema_for_slug("realestate") == "t_realestate"
    assert slug_from_schema("t_realestate") == "realestate"


def test_slug_rejects_bad_charset():
    for bad in ("My-Client", "my-client", "1abc", "a", "x" * 32, "", "acme.evil", "т_кир"):
        with pytest.raises(ValueError):
            validate_slug(bad)


def test_slug_rejects_reserved():
    assert "admin" in RESERVED_SLUGS
    for r in ("admin", "www", "api", "shared", "public"):
        with pytest.raises(ValueError):
            validate_slug(r)


def test_schema_name_validation_blocks_injection():
    assert validate_schema_name("t_acme") == "t_acme"
    for bad in ("public", "t_acme; DROP TABLE x", "t_", "t_Acme", "acme"):
        with pytest.raises(ValueError):
            validate_schema_name(bad)


def test_slug_from_schema_rejects_foreign_schema():
    with pytest.raises(ValueError):
        slug_from_schema("public")
