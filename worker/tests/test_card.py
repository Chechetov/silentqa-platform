import tasks.card as card


CFG = {"card_extraction": {"label": "Карта приёма", "prompt": "извлеки карту",
                           "json_schema": {"type": "object"}}}
TRANSCRIPT = [{"speaker": "B", "text": "Жалобы на сухость во рту"},
              {"speaker": "A", "text": "Да, давно"}]


def test_no_op_without_config():
    assert card.run_card_extraction(TRANSCRIPT, {"id": "realestate"}) is None


def test_extracts_with_config(monkeypatch):
    captured = {}

    def fake_sc(**kwargs):
        captured.update(kwargs)
        # Адаптор сам парсит JSON → фейк отдаёт уже распарсенный dict.
        return {"summary": "сбор анамнеза"}

    monkeypatch.setattr(card, "structured_completion", fake_sc)
    out = card.run_card_extraction(TRANSCRIPT, CFG)
    assert out == {"summary": "сбор анамнеза"}
    # schema + prompt from config were used
    assert captured["schema"] == {"type": "object"}
    assert captured["system"] == "извлеки карту"


def test_empty_transcript_returns_none():
    assert card.run_card_extraction([{"speaker": "A", "text": "  "}], CFG) is None


def test_llm_error_degrades_to_none(monkeypatch):
    def fake_sc(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(card, "structured_completion", fake_sc)
    assert card.run_card_extraction(TRANSCRIPT, CFG) is None
