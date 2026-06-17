"""build_session_metadata: санкционированный employee + сосуществование с broker."""
import uuid

from app.routes.sessions import build_session_metadata
from app.schemas import BrokerInfo


def _broker():
    return BrokerInfo(id=uuid.uuid4(), amocrm_user_id=42, email="b@x.io", name="Брокер Б")


def test_employee_kept_trimmed():
    meta = build_session_metadata({"source": "desktop-app", "employee": "  Иванов  "}, None)
    assert meta["employee"] == "Иванов"
    assert meta["source"] == "desktop-app"


def test_employee_empty_dropped():
    assert "employee" not in build_session_metadata({"employee": "   "}, None)
    assert "employee" not in build_session_metadata({"employee": ""}, None)


def test_employee_nonstring_dropped():
    assert "employee" not in build_session_metadata({"employee": {"x": 1}}, None)
    assert "employee" not in build_session_metadata({"employee": 123}, None)


def test_employee_truncated_to_120():
    meta = build_session_metadata({"employee": "Я" * 500}, None)
    assert len(meta["employee"]) == 120


def test_server_owned_stripped_even_if_client_sends():
    meta = build_session_metadata(
        {"company_id": "evil", "scenario_id": "evil", "broker_id": "x", "employee": "Пётр"}, None)
    assert "company_id" not in meta and "scenario_id" not in meta and "broker_id" not in meta
    assert meta["employee"] == "Пётр"


def test_broker_attribution_when_present():
    meta = build_session_metadata({"source": "amocrm"}, _broker())
    assert meta["broker_name"] == "Брокер Б"
    assert meta["amocrm_user_id"] == 42
    assert meta["responsible_user_id"] == 42
    assert "broker_id" in meta


def test_employee_and_broker_coexist():
    meta = build_session_metadata({"employee": "Иванов"}, _broker())
    assert meta["employee"] == "Иванов"
    assert meta["broker_name"] == "Брокер Б"


def test_appointment_type_passthrough():
    # appointment_type — НЕ server-owned (используется задачей 5 в finish_session).
    # Должен доживать до сохранения, не вычищаться.
    meta = build_session_metadata({"appointment_type": "consultation"}, None)
    assert meta["appointment_type"] == "consultation"
