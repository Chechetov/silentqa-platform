"""Юнит-тесты авто-классификатора типа созвона (без реального LLM-вызова)."""
from tasks import classify

SCENARIOS = [
    {"id": "client", "name": "Клиентский", "classify": {"hint": "продажа/поддержка"}},
    {"id": "partner", "name": "Партнёрский", "classify": {"hint": "проекты и идеи"}},
]


def test_none_with_fewer_than_two_classifiable():
    # только один сценарий с hint
    assert classify.classify_call_type([{"text": "привет"}], SCENARIOS[:1]) is None
    # два сценария, но без classify.hint
    assert classify.classify_call_type(
        [{"text": "привет"}],
        [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
    ) is None


def test_none_on_empty_transcript():
    assert classify.classify_call_type([], SCENARIOS) is None
    assert classify.classify_call_type([{"text": "   "}], SCENARIOS) is None


def test_picks_type_and_restricts_enum(monkeypatch):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {"type": "partner", "confidence": 0.9, "reason": "обсуждали проекты"}

    monkeypatch.setattr(classify, "structured_completion", fake)
    res = classify.classify_call_type([{"text": "давай обсудим проекты и идеи"}], SCENARIOS)
    assert res["type"] == "partner"
    # enum ограничен ровно id классифицируемых сценариев
    assert set(captured["schema"]["properties"]["type"]["enum"]) == {"client", "partner"}


def test_rejects_out_of_enum_answer(monkeypatch):
    monkeypatch.setattr(
        classify, "structured_completion",
        lambda **k: {"type": "bogus", "confidence": 1.0, "reason": None},
    )
    assert classify.classify_call_type([{"text": "x"}], SCENARIOS) is None


def test_none_on_llm_exception(monkeypatch):
    def boom(**k):
        raise RuntimeError("api down")

    monkeypatch.setattr(classify, "structured_completion", boom)
    assert classify.classify_call_type([{"text": "x"}], SCENARIOS) is None
