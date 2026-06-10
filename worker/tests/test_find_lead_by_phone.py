"""Tests for AmoCRM lead resolution by phone.

Regression context: AmoCRM contact search is substring-based, but contact
cards store Russian numbers with the trunk prefix '8' (e.g. 89281304030)
while call notes carry the dialed E.164 form +79281304030. The old code
normalised 8->7 and searched by '79281304030', which is NOT a substring of
'89281304030' (different leading digit) -> AmoCRM returns 204 -> the lead is
never found and the AI score is never pushed back to the deal.

The fix: search AmoCRM by the last 10 digits (the subscriber number), which
is a substring of every stored variant (8XXXXXXXXXX, 7XXXXXXXXXX, +7XXXX...).
"""
from unittest.mock import MagicMock

import tasks.amocrm_sync as amo


def _resp(status, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    return r


def test_search_query_uses_last_10_digits(monkeypatch):
    """find_lead_by_phone must query AmoCRM contacts by the 10-digit subscriber
    number so that contacts stored with the '8' trunk prefix still match."""
    captured = {}

    def fake_request(method, url, **kwargs):
        if "/contacts" in url:
            captured["query"] = kwargs.get("params", {}).get("query")
            # Contact stored as 8XXXXXXXXXX, linked to active lead 12172685
            return _resp(200, {"_embedded": {"contacts": [
                {"_embedded": {"leads": [{"id": 12172685}]}}
            ]}})
        if "/leads" in url:
            return _resp(200, {"_embedded": {"leads": [
                {"id": 12172685, "status_id": 75869534, "updated_at": 1780045046}
            ]}})
        return _resp(204)

    monkeypatch.setattr(amo, "_get_access_token", lambda: "token")
    monkeypatch.setattr(amo, "_amo_request", fake_request)

    lead_id = amo.find_lead_by_phone("+79281304030")

    assert captured["query"] == "9281304030", (
        f"expected 10-digit subscriber number, got {captured['query']!r}"
    )
    assert lead_id == 12172685
