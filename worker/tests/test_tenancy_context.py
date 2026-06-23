"""Tests for tenancy.context — request/task-scoped tenant state."""
import pytest

from tenancy.context import (
    TenantContextError,
    get_tenant_schema,
    get_tenant_slug,
    require_tenant_schema,
    require_tenant_slug,
    reset_tenant_schema,
    set_tenant_schema,
)


def test_default_is_unset():
    assert get_tenant_schema() is None
    assert get_tenant_slug() is None
    with pytest.raises(TenantContextError):
        require_tenant_schema()
    with pytest.raises(TenantContextError):
        require_tenant_slug()


def test_set_get_reset():
    token = set_tenant_schema("t_acme")
    try:
        assert get_tenant_schema() == "t_acme"
        assert require_tenant_schema() == "t_acme"
        assert get_tenant_slug() == "acme"
        assert require_tenant_slug() == "acme"
    finally:
        reset_tenant_schema(token)
    assert get_tenant_schema() is None


def test_set_validates_schema_name():
    with pytest.raises(ValueError):
        set_tenant_schema("public")
    with pytest.raises(ValueError):
        set_tenant_schema("t_acme; DROP SCHEMA shared")


def test_set_none_is_platform_context():
    token = set_tenant_schema(None)
    try:
        assert get_tenant_schema() is None
    finally:
        reset_tenant_schema(token)
