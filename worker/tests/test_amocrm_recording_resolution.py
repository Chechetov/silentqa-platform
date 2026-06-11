"""Tests for late-recording resolution in process_amocrm_call.

Regression context: a call whose recording is published *after* its AmoCRM
start-event has aged past the 15-minute poll window used to be dropped
entirely — the poll skipped notes without a recording URL, so the call never
entered amocrm_calls and the retry layer had nothing to retry. This is
fatal for long calls (a 30-minute call's event ages out before it even ends).

The fix: ingest the call immediately (recording_url may be empty) and let
process_amocrm_call resolve the recording later by re-reading the note via
the persisted entity reference, parking the row as 'awaiting_recording'
between attempts.
"""
from unittest.mock import MagicMock

import tasks.amocrm_poll as ap


def _fake_conn(row):
    """psycopg2-connection mock that yields `row` from cursor.fetchone()."""
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


def _row(recording_url="", retry_count=0, entity_type="contacts", entity_id=11099619):
    """A row in the column order of the SELECT in process_amocrm_call."""
    return (50157725, 11425157, "+79663348000", "out", 1805,
            recording_url, retry_count, 13569418, entity_type, entity_id)


def test_missing_recording_parks_call_as_awaiting(monkeypatch):
    """Empty recording_url + note still has no link → status 'awaiting_recording'."""
    updates = []
    monkeypatch.setattr(ap, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(ap, "tenant_connect",
                        lambda *a, **k: _fake_conn(_row(retry_count=2)))
    monkeypatch.setattr(ap, "get_note_details", lambda *a, **k: {"params": {"link": ""}})
    monkeypatch.setattr(ap, "_update_call_status",
                        lambda cid, status, **kw: updates.append((cid, status, kw)))

    ap.process_amocrm_call(call_id=42)

    assert len(updates) == 1
    cid, status, kw = updates[0]
    assert cid == 42
    assert status == "awaiting_recording"
    assert kw["retry_count"] == 3                       # incremented from 2
    assert kw["error_message"] == "recording_not_published"


def test_missing_recording_resolved_on_retry(monkeypatch, tmp_path):
    """Empty recording_url + note now has a link → URL persisted, download begins."""
    updates = []
    monkeypatch.setattr(ap, "AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(ap, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(ap, "tenant_connect",
                        lambda *a, **k: _fake_conn(_row()))
    monkeypatch.setattr(ap, "get_note_details",
                        lambda *a, **k: {"params": {"link": "https://media.example/rec.mp3"}})
    monkeypatch.setattr(ap, "_update_call_status",
                        lambda cid, status, **kw: updates.append((cid, status, kw)))
    # Stop right after URL resolution by making the download fail fast.
    monkeypatch.setattr(ap, "download_recording", lambda url, dest: False)

    ap.process_amocrm_call(call_id=42)

    # First status update persists the resolved URL alongside 'downloading'
    # and clears the stale 'recording_not_published' marker.
    cid, status, kw = updates[0]
    assert status == "downloading"
    assert kw["recording_url"] == "https://media.example/rec.mp3"
    assert kw["error_message"] is None


def test_present_recording_skips_resolution(monkeypatch, tmp_path):
    """recording_url already stored → the note is NOT re-fetched."""
    fetched = []
    monkeypatch.setattr(ap, "AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(ap, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(ap, "tenant_connect",
                        lambda *a, **k: _fake_conn(_row(recording_url="https://media.example/x.mp3")))
    monkeypatch.setattr(ap, "_update_call_status", lambda *a, **k: None)
    monkeypatch.setattr(ap, "download_recording", lambda url, dest: False)
    monkeypatch.setattr(ap, "get_note_details", lambda *a, **k: fetched.append(1))

    ap.process_amocrm_call(call_id=42)

    assert fetched == []                                # resolution path not entered
