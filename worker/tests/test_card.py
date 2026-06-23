from unittest import mock

import tasks.card as card


CFG = {"card_extraction": {"label": "Карта приёма", "prompt": "извлеки карту",
                           "json_schema": {"type": "object"}}}
TRANSCRIPT = [{"speaker": "B", "text": "Жалобы на сухость во рту"},
              {"speaker": "A", "text": "Да, давно"}]


def test_no_op_without_config():
    assert card.run_card_extraction(TRANSCRIPT, {"id": "realestate"}) is None


def test_extracts_with_config(monkeypatch):
    fake_resp = mock.MagicMock()
    fake_resp.output_text = '{"summary": "сбор анамнеза"}'
    fake_client = mock.MagicMock()
    fake_client.responses.create.return_value = fake_resp
    monkeypatch.setattr(card, "OpenAI", lambda: fake_client)
    out = card.run_card_extraction(TRANSCRIPT, CFG)
    assert out == {"summary": "сбор анамнеза"}
    # schema + prompt from config were used
    _, kwargs = fake_client.responses.create.call_args
    assert kwargs["text"]["format"]["schema"] == {"type": "object"}
    assert kwargs["input"][0]["content"] == "извлеки карту"


def test_empty_transcript_returns_none():
    assert card.run_card_extraction([{"speaker": "A", "text": "  "}], CFG) is None


def test_llm_error_degrades_to_none(monkeypatch):
    fake_client = mock.MagicMock()
    fake_client.responses.create.side_effect = RuntimeError("boom")
    monkeypatch.setattr(card, "OpenAI", lambda: fake_client)
    assert card.run_card_extraction(TRANSCRIPT, CFG) is None
