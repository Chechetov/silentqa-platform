import tasks.card as card


_LAST_KWARGS = {}


def _patch(monkeypatch, result):
    """Мокаем llm.egress-адаптор: фейк отдаёт уже распарсенный dict
    (адаптор сам парсит JSON) и запоминает kwargs вызова."""
    def fake_sc(**kwargs):
        _LAST_KWARGS.clear()
        _LAST_KWARGS.update(kwargs)
        return result
    monkeypatch.setattr(card, "structured_completion", fake_sc)


def test_scenario_card_wins(monkeypatch):
    _patch(monkeypatch, {"x": 1})
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    scen = {"id": "s1", "card_extraction": {"prompt": "SCENARIO", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, scen)
    assert out == {"x": 1}
    assert _LAST_KWARGS["system"] == "SCENARIO"


def test_falls_back_to_config(monkeypatch):
    _patch(monkeypatch, {"y": 2})
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, {"id": "s1"})
    assert out == {"y": 2}
    assert _LAST_KWARGS["system"] == "CONFIG"


def test_scenario_none_uses_config(monkeypatch):
    _patch(monkeypatch, {"z": 3})
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, None)
    assert out == {"z": 3}


def test_none_when_no_card():
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], {}, {"id": "s1"})
    assert out is None


def test_scenario_explicit_null_opts_out(monkeypatch):
    # Явный card_extraction: null у сценария = отказ от карты, БЕЗ фолбэка на config.
    called = {"n": 0}

    def fake_sc(**kwargs):
        called["n"] += 1
        raise AssertionError("structured_completion не должен вызываться при card_extraction: null")
    monkeypatch.setattr(card, "structured_completion", fake_sc)
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, {"id": "s1", "card_extraction": None})
    assert out is None
    assert called["n"] == 0   # opt-out: адаптор не тронут (нет фолбэка на config)


def test_has_card_extraction():
    cfg = {"card_extraction": {"prompt": "C", "json_schema": {"type": "object"}}}
    # config-level есть, сценарий без ключа → есть
    assert card.has_card_extraction(cfg, {"id": "s1"}) is True
    assert card.has_card_extraction(cfg, None) is True
    # сценарий явно отключил карту → нет (несмотря на config-level)
    assert card.has_card_extraction(cfg, {"id": "s1", "card_extraction": None}) is False
    # ни config, ни scenario → нет
    assert card.has_card_extraction({}, {"id": "s1"}) is False


def test_delete_results_removes_card(tmp_path, monkeypatch):
    from tasks import pipeline
    from tenancy.context import set_tenant_schema, reset_tenant_schema

    monkeypatch.setattr(pipeline, "RESULTS_PATH", tmp_path)
    tok = set_tenant_schema("t_acme")
    try:
        pipeline.save_results("sess1", "card", {"a": 1})
        assert list(tmp_path.rglob("card.json")), "card.json должен был создаться"
        pipeline.delete_results("sess1", "card")
        assert not list(tmp_path.rglob("card.json"))
        pipeline.delete_results("sess1", "card")   # идемпотентно
    finally:
        reset_tenant_schema(tok)
