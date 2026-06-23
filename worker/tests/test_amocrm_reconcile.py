"""Tests for the self-healing reconciliation layer.

Covers:
  - _ingest_call_event: the shared poll/reconcile ingest helper (dedup, skip our
    own notes, enqueue only on a fresh insert).
  - reconcile_amocrm_calls: deep-sweep ingests only notes missing from the DB,
    skips known ones before the expensive note fetch, alerts on recovery, and
    never raises into Beat.
  - send_alert: degrades to a log (no raise) when Telegram is unconfigured.
"""
from unittest.mock import MagicMock

import tasks.amocrm_poll as ap
import tasks.amocrm_reconcile as rec
import tasks.amocrm_alerts as al


# ----- _ingest_call_event (shared helper) -----------------------------------

def _event(note_id=900, etype="contact", eid=11):
    return {"note_id": note_id, "entity_id": eid, "entity_type": etype, "event_type": "outgoing_call"}


def test_ingest_event_new_inserts_and_enqueues(monkeypatch):
    enqueued = []
    monkeypatch.setattr(ap, "get_note_details",
                        lambda *a, **k: {"note_type": "call_out", "params": {"link": "u", "duration": 50}})
    monkeypatch.setattr(ap, "_insert_call", lambda **k: 777)            # fresh row
    monkeypatch.setattr(ap.process_amocrm_call, "delay",
                        lambda cid, **kw: enqueued.append(cid))

    assert ap._ingest_call_event(_event()) == 777
    assert enqueued == [777]


def test_ingest_event_duplicate_no_enqueue(monkeypatch):
    enqueued = []
    monkeypatch.setattr(ap, "get_note_details",
                        lambda *a, **k: {"note_type": "call_out", "params": {"link": "u"}})
    monkeypatch.setattr(ap, "_insert_call", lambda **k: None)           # ON CONFLICT -> already exists
    monkeypatch.setattr(ap.process_amocrm_call, "delay",
                        lambda cid, **kw: enqueued.append(cid))

    assert ap._ingest_call_event(_event()) is None
    assert enqueued == []


def test_ingest_event_skips_own_notes(monkeypatch):
    inserted = []
    monkeypatch.setattr(ap, "get_note_details",
                        lambda *a, **k: {"note_type": "call_out", "params": {"source": "Rogov AI"}})
    monkeypatch.setattr(ap, "_insert_call", lambda **k: inserted.append(1))

    assert ap._ingest_call_event(_event()) is None
    assert inserted == []                                              # never attempted insert


# ----- reconcile deep-sweep --------------------------------------------------

def test_reconcile_ingests_only_missing_notes(monkeypatch):
    events = [_event(note_id=100), _event(note_id=200), _event(note_id=300)]
    monkeypatch.setattr(rec, "get_recent_call_events", lambda since: events)
    monkeypatch.setattr(rec, "_existing_note_ids", lambda ids: {200})   # 200 already in DB
    ingested = []
    monkeypatch.setattr(rec, "_ingest_call_event",
                        lambda e: ingested.append(e["note_id"]) or 555)
    monkeypatch.setattr(rec, "_stuck_counts",
                        lambda: {"recording_timeout": 0, "failed_terminal": 0, "last_ingest_age_min": 3.0})
    alerts = []
    monkeypatch.setattr(rec, "send_alert", lambda msg: alerts.append(msg))

    out = rec._reconcile_for_current_tenant()

    assert ingested == [100, 300]            # 200 skipped (known), no get_note_details for it
    assert out["recovered"] == 2
    assert out["scanned"] == 3
    assert len(alerts) == 1                  # recovered>0 -> one alert


def test_reconcile_no_recovery_no_alert(monkeypatch):
    events = [_event(note_id=100)]
    monkeypatch.setattr(rec, "get_recent_call_events", lambda since: events)
    monkeypatch.setattr(rec, "_existing_note_ids", lambda ids: {100})   # all known
    monkeypatch.setattr(rec, "_ingest_call_event", lambda e: (_ for _ in ()).throw(AssertionError("should not ingest")))
    monkeypatch.setattr(rec, "_stuck_counts",
                        lambda: {"recording_timeout": 2, "failed_terminal": 1, "last_ingest_age_min": 5.0})
    alerts = []
    monkeypatch.setattr(rec, "send_alert", lambda msg: alerts.append(msg))

    out = rec._reconcile_for_current_tenant()

    assert out["recovered"] == 0
    assert out["recording_timeout"] == 2     # stuck counts surfaced in metrics
    assert alerts == []                      # stuck-terminal alone does not spam alerts


def test_reconcile_batch_cap_limits_burst(monkeypatch):
    """More missing notes than the cap -> ingest only up to the cap this cycle."""
    monkeypatch.setattr(rec, "RECONCILE_BATCH_LIMIT", 3)
    events = [_event(note_id=n) for n in range(10)]   # 10 all-unknown notes
    monkeypatch.setattr(rec, "get_recent_call_events", lambda since: events)
    monkeypatch.setattr(rec, "_existing_note_ids", lambda ids: set())
    ingested = []
    monkeypatch.setattr(rec, "_ingest_call_event", lambda e: ingested.append(e["note_id"]) or 1)
    monkeypatch.setattr(rec, "_stuck_counts",
                        lambda: {"recording_timeout": 0, "failed_terminal": 0, "last_ingest_age_min": 0.0})
    monkeypatch.setattr(rec, "send_alert", lambda msg: None)

    out = rec._reconcile_for_current_tenant()

    assert len(ingested) == 3            # stopped at the cap
    assert out["recovered"] == 3
    assert out["capped"] is True


def test_reconcile_never_raises_into_beat(monkeypatch):
    def boom(since):
        raise RuntimeError("AmoCRM/DB down")
    monkeypatch.setattr(rec, "get_recent_call_events", boom)

    out = rec._reconcile_for_current_tenant()
    assert out == {"error": True}            # swallowed; Beat keeps running


# ----- alert fallback --------------------------------------------------------

def test_send_alert_no_telegram_degrades_to_log(monkeypatch):
    monkeypatch.setattr(al, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(al, "TELEGRAM_CHAT_ID", "")
    # must return False and NOT raise / NOT attempt an HTTP call
    monkeypatch.setattr(al.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP when unconfigured")))
    assert al.send_alert("something") is False
