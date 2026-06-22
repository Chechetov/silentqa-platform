"""ASR-сравнение: прогон звонка несколькими движками → варианты на диске + индекс.

Pure unit: monkeypatch путей хранения и transcribe_audio (никаких реальных
движков/сети). Проверяем: каждый успешный движок пишет transcript_<engine>.json,
ошибка одного движка не валит остальные, индекс transcript_variants.json со
статистикой сохранён.
"""
import json
from pathlib import Path

import pytest

import tasks.transcribe_compare as tc
from tenancy.context import reset_tenant_schema, set_tenant_schema


@pytest.fixture()
def acme_ctx():
    token = set_tenant_schema("t_acme")
    yield
    reset_tenant_schema(token)


def _fake_transcribe(audio_path, word_boost=None, engine_override=None, fallback=True):
    # honest comparison must disable fallback
    assert fallback is False
    if engine_override == "assemblyai":
        raise RuntimeError("assemblyai key missing")
    if engine_override == "elevenlabs":
        return [
            {"speaker": "speaker_0", "start": 0.0, "end": 1.0, "text": "привет"},
            {"speaker": "speaker_1", "start": 1.0, "end": 2.0, "text": "здравствуйте"},
        ]
    # whisper — no speakers
    return [{"start": 0.0, "end": 2.0, "text": "привет здравствуйте"}]


@pytest.fixture()
def patched(tmp_path, monkeypatch, acme_ctx):
    monkeypatch.setattr(tc, "AUDIO_PATH", str(tmp_path / "audio"))
    monkeypatch.setattr(tc, "RESULTS_PATH", str(tmp_path / "results"))
    monkeypatch.setattr(tc, "transcribe_audio", _fake_transcribe)
    monkeypatch.setattr(tc, "load_company_config", lambda cid: {"id": cid or "default"})
    monkeypatch.setattr(tc, "tenant_company_config_id", lambda: "default")
    # merged audio must exist
    sdir = tmp_path / "audio" / "acme" / "sessions" / "sid-1"
    sdir.mkdir(parents=True)
    (sdir / "full.wav").write_bytes(b"\x00")
    return tmp_path


def _results_dir(tmp_path):
    return tmp_path / "results" / "acme" / "sid-1"


def test_writes_variant_per_engine(patched):
    tc.run_compare("sid-1", ["whisper", "elevenlabs"])
    rd = _results_dir(patched)
    assert (rd / "transcript_whisper.json").exists()
    assert (rd / "transcript_elevenlabs.json").exists()
    el = json.loads((rd / "transcript_elevenlabs.json").read_text())
    assert len(el) == 2 and el[0]["speaker"] == "speaker_0"


def test_failed_engine_does_not_break_others(patched):
    index = tc.run_compare("sid-1", ["whisper", "assemblyai", "elevenlabs"])
    rd = _results_dir(patched)
    # whisper + elevenlabs succeeded, assemblyai errored but didn't crash the run
    assert (rd / "transcript_whisper.json").exists()
    assert (rd / "transcript_elevenlabs.json").exists()
    assert not (rd / "transcript_assemblyai.json").exists()
    assert index["assemblyai"]["status"] == "error"
    assert "assemblyai" in index["assemblyai"]["error"]
    assert index["whisper"]["status"] == "ok"


def test_index_has_stats(patched):
    index = tc.run_compare("sid-1", ["whisper", "elevenlabs"])
    rd = _results_dir(patched)
    saved = json.loads((rd / "transcript_variants.json").read_text())
    assert saved == index
    assert index["elevenlabs"]["has_speakers"] is True
    assert index["elevenlabs"]["segments"] == 2
    assert index["whisper"]["has_speakers"] is False
    assert index["whisper"]["chars"] > 0


def test_unsupported_engines_rejected(patched):
    with pytest.raises(ValueError):
        tc.run_compare("sid-1", ["bogus"])


def test_missing_audio_raises(tmp_path, monkeypatch, acme_ctx):
    monkeypatch.setattr(tc, "AUDIO_PATH", str(tmp_path / "audio"))
    monkeypatch.setattr(tc, "RESULTS_PATH", str(tmp_path / "results"))
    monkeypatch.setattr(tc, "transcribe_audio", _fake_transcribe)
    monkeypatch.setattr(tc, "load_company_config", lambda cid: {})
    monkeypatch.setattr(tc, "tenant_company_config_id", lambda: "default")
    with pytest.raises(FileNotFoundError):
        tc.run_compare("sid-1", ["whisper"])
