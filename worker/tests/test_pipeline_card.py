import tasks.card as card

DENTAL = {"card_extraction": {"label": "Карта приёма", "prompt": "p", "json_schema": {"type": "object"}}}
REALESTATE = {"id": "realestate"}


def test_card_runs_for_dental_config(monkeypatch):
    # Адаптор сам парсит JSON → фейк отдаёт уже распарсенный dict.
    monkeypatch.setattr(card, "structured_completion", lambda **kw: {"summary": "ok"})
    assert card.run_card_extraction([{"speaker": "A", "text": "жалоба"}], DENTAL) == {"summary": "ok"}


def test_card_skipped_for_realestate(monkeypatch):
    # realestate config has no card_extraction -> no LLM call, returns None
    def fake_sc(**kw):
        raise AssertionError("must not call LLM")
    monkeypatch.setattr(card, "structured_completion", fake_sc)
    assert card.run_card_extraction([{"speaker": "A", "text": "x"}], REALESTATE) is None
