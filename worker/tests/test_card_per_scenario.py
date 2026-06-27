from tasks import card


class _Resp:
    def __init__(self, text):
        self.output_text = text


class _Client:
    last_kwargs = None

    def __init__(self, text):
        self._text = text
        self.responses = self

    def create(self, **kw):
        _Client.last_kwargs = kw
        return _Resp(self._text)


def _patch(monkeypatch, text):
    monkeypatch.setattr(card, "OpenAI", lambda: _Client(text))


def test_scenario_card_wins(monkeypatch):
    _patch(monkeypatch, '{"x": 1}')
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    scen = {"id": "s1", "card_extraction": {"prompt": "SCENARIO", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, scen)
    assert out == {"x": 1}
    assert _Client.last_kwargs["input"][0]["content"] == "SCENARIO"


def test_falls_back_to_config(monkeypatch):
    _patch(monkeypatch, '{"y": 2}')
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, {"id": "s1"})
    assert out == {"y": 2}
    assert _Client.last_kwargs["input"][0]["content"] == "CONFIG"


def test_scenario_none_uses_config(monkeypatch):
    _patch(monkeypatch, '{"z": 3}')
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, None)
    assert out == {"z": 3}


def test_none_when_no_card():
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], {}, {"id": "s1"})
    assert out is None


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
