from unittest import mock
import tasks.card as card

DENTAL = {"card_extraction": {"label": "Карта приёма", "prompt": "p", "json_schema": {"type": "object"}}}
REALESTATE = {"id": "realestate"}


def test_card_runs_for_dental_config(monkeypatch):
    fake_resp = mock.MagicMock(); fake_resp.output_text = '{"summary": "ok"}'
    fc = mock.MagicMock(); fc.responses.create.return_value = fake_resp
    monkeypatch.setattr(card, "OpenAI", lambda: fc)
    assert card.run_card_extraction([{"speaker": "A", "text": "жалоба"}], DENTAL) == {"summary": "ok"}


def test_card_skipped_for_realestate(monkeypatch):
    # realestate config has no card_extraction -> no OpenAI call, returns None
    monkeypatch.setattr(card, "OpenAI", lambda: (_ for _ in ()).throw(AssertionError("must not call LLM")))
    assert card.run_card_extraction([{"speaker": "A", "text": "x"}], REALESTATE) is None
