"""Пост-ASR гейт «нет диалога» (ревью D-2): гудки/автоответчик/монолог не жгут LLM.

Матрица условий на чистой функции `_no_dialogue_gate` — все побочные эффекты
(save_results / record_quality_result / _push_to_amocrm / _get_audio_duration)
замоканы, так что тенант-контекст и БД не нужны.
"""
import pytest

import tasks.pipeline as pl


class StubTask:
    def __init__(self):
        self.states = []

    def update_state(self, state=None, meta=None):
        self.states.append((state, meta))


@pytest.fixture
def gate_env(monkeypatch):
    """Замок всех побочных эффектов гейта; возвращает захваченные вызовы."""
    saved = []
    recorded = []
    pushed = []
    monkeypatch.setattr(pl, "save_results", lambda sid, key, data: saved.append((sid, key, data)))
    monkeypatch.setattr(pl, "record_quality_result",
                        lambda sid, report, **kw: recorded.append((sid, report, kw)) or [])
    monkeypatch.setattr(pl, "_push_to_amocrm", lambda *a, **k: pushed.append((a, k)))
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 42.0)
    # На всякий случай — дефолт «гейт включён» (среда теста может нести NO_DIALOGUE_GATE)
    monkeypatch.delenv("NO_DIALOGUE_GATE", raising=False)
    monkeypatch.delenv("NO_DIALOGUE_MIN_SEGMENTS", raising=False)
    monkeypatch.delenv("NO_DIALOGUE_MIN_CHARS", raising=False)
    return {"saved": saved, "recorded": recorded, "pushed": pushed}


def _dialogue(n_segments: int, speakers: int, chars_each: int) -> list:
    """Синтетический транскрипт: n сегментов, круговой набор спикеров, длина реплики."""
    out = []
    for i in range(n_segments):
        out.append({
            "start": i, "end": i + 1,
            "speaker": f"S{i % speakers}",
            "text": "а" * chars_each,
        })
    return out


def test_single_speaker_fires_gate(gate_env):
    task = StubTask()
    # 10 длинных реплик, но все от одного спикера → диалог не состоялся
    transcript = _dialogue(n_segments=10, speakers=1, chars_each=50)
    gate = pl._no_dialogue_gate(task, "sid-1", "/tmp/a.wav", None, {}, transcript)
    assert gate is not None
    assert gate["quality_report"]["skip_reason"] == "no_dialogue"
    assert gate["quality_report"]["overall_score"] is None
    assert gate["quality_report"]["score_version"] == "no_dialogue_v1"
    assert gate["quality_report"]["dialogue_stats"] == {"speakers": 1, "segments": 10, "chars": 500}


def test_too_few_segments_fires_gate(gate_env):
    task = StubTask()
    # 2 спикера, длинные реплики (>200 символов суммарно), но всего 3 реплики (<4)
    transcript = _dialogue(n_segments=3, speakers=2, chars_each=100)
    gate = pl._no_dialogue_gate(task, "sid-2", "/tmp/a.wav", None, {}, transcript)
    assert gate is not None
    assert gate["quality_report"]["skip_reason"] == "no_dialogue"


def test_too_few_chars_fires_gate(gate_env):
    task = StubTask()
    # 2 спикера, 6 реплик, но суммарно мало символов (<200)
    transcript = _dialogue(n_segments=6, speakers=2, chars_each=5)
    gate = pl._no_dialogue_gate(task, "sid-2c", "/tmp/a.wav", None, {}, transcript)
    assert gate is not None
    assert gate["quality_report"]["skip_reason"] == "no_dialogue"


def test_real_dialogue_passes(gate_env):
    task = StubTask()
    # 2 спикера, 8 длинных реплик, много символов → полноценный диалог, гейт молчит
    transcript = _dialogue(n_segments=8, speakers=2, chars_each=50)
    gate = pl._no_dialogue_gate(task, "sid-3", "/tmp/a.wav", None, {}, transcript)
    assert gate is None
    assert gate_env["recorded"] == []  # ничего не записали — идём в LLM


def test_empty_segments_ignored(gate_env):
    task = StubTask()
    # Пустые/пробельные реплики не считаются: 2 «спикера», но текст пустой
    transcript = [
        {"start": 0, "end": 1, "speaker": "S0", "text": "   "},
        {"start": 1, "end": 2, "speaker": "S1", "text": ""},
        {"start": 2, "end": 3, "speaker": "S0", "text": None},
    ]
    gate = pl._no_dialogue_gate(task, "sid-3b", "/tmp/a.wav", None, {}, transcript)
    assert gate is not None
    assert gate["quality_report"]["dialogue_stats"] == {"speakers": 0, "segments": 0, "chars": 0}


def test_gate_disabled_returns_none(gate_env, monkeypatch):
    monkeypatch.setenv("NO_DIALOGUE_GATE", "0")
    task = StubTask()
    transcript = _dialogue(n_segments=10, speakers=1, chars_each=50)  # заведомо недиалог
    gate = pl._no_dialogue_gate(task, "sid-4", "/tmp/a.wav", None, {}, transcript)
    assert gate is None
    assert gate_env["saved"] == []
    assert gate_env["recorded"] == []


def test_returns_transcript_and_use_extended_off(gate_env):
    task = StubTask()
    transcript = _dialogue(n_segments=2, speakers=1, chars_each=10)
    gate = pl._no_dialogue_gate(task, "sid-5", "/tmp/a.wav", None, {}, transcript)
    assert gate["transcript_with_speakers"] is transcript
    assert gate["use_extended"] is False
    # use_extended=False → в AmoCRM не пушим
    assert gate_env["pushed"] == []


def test_use_extended_scenario_pushes(gate_env):
    task = StubTask()
    transcript = _dialogue(n_segments=2, speakers=1, chars_each=10)
    scenario = {"prompt": "playbook..."}
    gate = pl._no_dialogue_gate(task, "sid-6", "/tmp/a.wav", scenario, {"lead_id": 1}, transcript)
    assert gate["use_extended"] is True
    assert len(gate_env["pushed"]) == 1
    assert ("PROGRESS", {"step": "amocrm_sync", "progress": 95}) in task.states


def test_record_quality_result_skip_reason(gate_env):
    task = StubTask()
    transcript = _dialogue(n_segments=1, speakers=1, chars_each=10)
    pl._no_dialogue_gate(task, "sid-7", "/tmp/a.wav", None, {}, transcript)
    assert len(gate_env["recorded"]) == 1
    sid, report, kw = gate_env["recorded"][0]
    assert sid == "sid-7"
    assert kw["skip_reason"] == "no_dialogue"
    assert kw["duration_seconds"] == 42.0  # из замоканного _get_audio_duration
    # отчёт сохранён на диск под ключом quality
    assert ("sid-7", "quality", report) in gate_env["saved"]


def test_custom_thresholds_env(gate_env, monkeypatch):
    # Поднимаем планку сегментов: 5 реплик двух спикеров при min_segments=6 → гейт
    monkeypatch.setenv("NO_DIALOGUE_MIN_SEGMENTS", "6")
    monkeypatch.setenv("NO_DIALOGUE_MIN_CHARS", "0")
    task = StubTask()
    transcript = _dialogue(n_segments=5, speakers=2, chars_each=50)
    gate = pl._no_dialogue_gate(task, "sid-8", "/tmp/a.wav", None, {}, transcript)
    assert gate is not None
    assert gate["quality_report"]["skip_reason"] == "no_dialogue"
