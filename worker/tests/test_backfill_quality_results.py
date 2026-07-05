"""Бэкфилл: сборка строки из файлов + dry-run/apply + скип без сессии."""
import json

import scripts.backfill_quality_results as bf


def _mk_session_dir(tmp_path, slug, sid, quality, transcript=None, sentiment=None, card=None):
    d = tmp_path / slug / sid
    d.mkdir(parents=True)
    (d / "quality.json").write_text(json.dumps(quality, ensure_ascii=False), encoding="utf-8")
    if transcript is not None:
        (d / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    if sentiment is not None:
        (d / "sentiment.json").write_text(json.dumps(sentiment), encoding="utf-8")
    if card is not None:
        (d / "card.json").write_text(json.dumps(card), encoding="utf-8")
    return d


def test_dry_run_counts_but_does_not_upsert(tmp_path, monkeypatch):
    _mk_session_dir(tmp_path, "acme", "sid-1", {"overall_score": 7, "version": 4})
    upserts = []
    monkeypatch.setattr(bf, "_upsert_from_files", lambda *a, **k: upserts.append(1) or True)
    monkeypatch.setattr(bf, "_session_exists", lambda sid: True)
    stats = bf.backfill_tenant("acme", tmp_path, apply=False)
    assert stats == {"found": 1, "upserted": 0, "skipped": 0}
    assert not upserts


def test_apply_upserts(tmp_path, monkeypatch):
    _mk_session_dir(tmp_path, "acme", "sid-1", {"overall_score": 7, "version": 4},
                    transcript=[{"speaker": "A", "start": 0, "end": 5, "text": "х"}],
                    sentiment=[{"sentiment": "positive"}])
    seen = {}
    monkeypatch.setattr(bf, "_upsert_from_files",
                        lambda sid, quality, transcript, sentiment, card: seen.update(
                            sid=sid, q=quality) or True)
    monkeypatch.setattr(bf, "_session_exists", lambda sid: True)
    stats = bf.backfill_tenant("acme", tmp_path, apply=True)
    assert stats["upserted"] == 1 and seen["sid"] == "sid-1"


def test_missing_session_row_skipped(tmp_path, monkeypatch):
    _mk_session_dir(tmp_path, "acme", "sid-ghost", {"overall_score": 5})
    monkeypatch.setattr(bf, "_session_exists", lambda sid: False)
    stats = bf.backfill_tenant("acme", tmp_path, apply=True)
    assert stats == {"found": 1, "upserted": 0, "skipped": 1}


def test_dir_without_quality_json_ignored(tmp_path, monkeypatch):
    (tmp_path / "acme" / "sid-noq").mkdir(parents=True)
    monkeypatch.setattr(bf, "_session_exists", lambda sid: True)
    stats = bf.backfill_tenant("acme", tmp_path, apply=True)
    assert stats == {"found": 0, "upserted": 0, "skipped": 0}
