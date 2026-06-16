import tasks.knowledge_base as kb


def test_merge_keyterms_into_word_boost(monkeypatch):
    from tasks.pipeline import _merge_kb_keyterms
    monkeypatch.setattr(kb, "kb_keyterms", lambda: ["Эталон", "квартира"])
    out = _merge_kb_keyterms(["квартира", "ипотека"])
    assert "Эталон" in out and out.count("квартира") == 1  # union + dedup


def test_merge_keyterms_survives_kb_error(monkeypatch):
    from tasks.pipeline import _merge_kb_keyterms

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(kb, "kb_keyterms", boom)
    assert _merge_kb_keyterms(["квартира"]) == ["квартира"]  # degrades to original
