"""Tests for two AmoCRM call-processing guarantees:

1. Inbound calls are not scored — there is no inbound rubric, so processing
   them produced a default-protocol score that was never pushed (silent waste).
   process_amocrm_call must short-circuit inbound calls explicitly, before any
   download/transcription, with a clear terminal status.

2. AmoCRM push failures must be retryable. The push happens inside the pipeline
   and its failure used to be swallowed (warning only) while the call was still
   marked 'completed' — losing the score forever. When a push was expected but
   no note landed, the call must be marked 'failed' so the retry layer re-runs it.
"""
from unittest.mock import MagicMock

import tasks.amocrm_poll as ap
import tasks.pipeline as pl
import tasks.company_config as cc


def _fake_conn(row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


def _row(direction="out", recording_url="https://media.example/rec.mp3"):
    # Column order of the SELECT in process_amocrm_call:
    # amo_note_id, lead_id, contact_phone, direction, duration, recording_url,
    # retry_count, responsible_user_id, entity_type, entity_id
    return (50157725, 0, "+79663348000", direction, 120,
            recording_url, 0, 13569418, "contacts", 11099619)


def test_inbound_call_skipped_without_download(monkeypatch):
    """Inbound call -> terminal 'skipped_inbound', no recording download."""
    updates = []
    downloaded = []
    monkeypatch.setattr(ap, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(ap, "tenant_connect",
                        lambda *a, **k: _fake_conn(_row(direction="in")))
    monkeypatch.setattr(ap, "_update_call_status",
                        lambda cid, status, **kw: updates.append((status, kw)))
    monkeypatch.setattr(ap, "download_recording", lambda *a, **k: downloaded.append(1) or True)
    monkeypatch.setattr(ap, "get_note_details", lambda *a, **k: downloaded.append(1) or {})

    ap.process_amocrm_call(call_id=42, tenant_schema="t_realestate")

    assert downloaded == []                       # never tried to fetch/download
    assert len(updates) == 1
    status, kw = updates[0]
    assert status == "skipped_inbound"


def _wire_full_path(monkeypatch, tmp_path, pushed: bool):
    """Mock the download->convert->pipeline path; pushed controls whether the
    session ends up with an amo_note_id (i.e. whether the push succeeded)."""
    updates = []
    monkeypatch.setattr(ap, "AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(ap, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(ap, "tenant_connect",
                        lambda *a, **k: _fake_conn(_row(direction="out")))
    monkeypatch.setattr(ap, "_update_call_status",
                        lambda cid, status, **kw: updates.append((status, kw)))
    monkeypatch.setattr(ap, "download_recording", lambda *a, **k: True)
    monkeypatch.setattr(ap, "_convert_to_wav", lambda *a, **k: True)
    monkeypatch.setattr(ap, "_create_session", lambda meta: "sess-1")
    monkeypatch.setattr(pl, "_run_pipeline",
                        lambda *a, **k: {"use_extended": True,
                                         "quality_report": {"overall_score": 7}})
    monkeypatch.setattr(pl, "update_session_status", lambda *a, **k: None)
    monkeypatch.setattr(pl, "_get_audio_duration", lambda *a, **k: 120.0)
    monkeypatch.setattr(pl, "_get_session_metadata",
                        lambda sid: {"amo_note_id": 999} if pushed else {})
    monkeypatch.setattr(cc, "load_company_config", lambda *a, **k: {})
    monkeypatch.setattr(cc, "get_scenario", lambda *a, **k: {"prompt": "x"})
    return updates


def test_push_failure_marks_call_failed(monkeypatch, tmp_path):
    """Push expected (use_extended, no skip) but no amo_note_id landed -> failed."""
    updates = _wire_full_path(monkeypatch, tmp_path, pushed=False)
    ap.process_amocrm_call(call_id=42, tenant_schema="t_realestate")
    final = updates[-1]
    assert final[0] == "failed"
    assert final[1].get("error_message") == "amocrm_push_failed"
    assert final[1].get("retry_count") == 1


def test_push_success_marks_call_completed(monkeypatch, tmp_path):
    """Push succeeded (amo_note_id present) -> completed."""
    updates = _wire_full_path(monkeypatch, tmp_path, pushed=True)
    ap.process_amocrm_call(call_id=42, tenant_schema="t_realestate")
    assert updates[-1][0] == "completed"
