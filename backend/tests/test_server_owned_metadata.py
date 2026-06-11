"""company_id/scenario_id — server-owned: клиентский ввод вычищается."""
from app.routes.sessions import _SERVER_OWNED_METADATA


def test_company_and_scenario_are_server_owned():
    assert "company_id" in _SERVER_OWNED_METADATA
    assert "scenario_id" in _SERVER_OWNED_METADATA
    # старый набор не потерян
    for k in ("broker_id", "amocrm_user_id", "broker_name", "responsible_user_id"):
        assert k in _SERVER_OWNED_METADATA
