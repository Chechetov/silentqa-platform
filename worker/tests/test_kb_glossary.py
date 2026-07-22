from tasks.knowledge_base import format_glossary


def test_glossary_empty_returns_empty():
    assert format_glossary([]) == ""


def test_glossary_renders_terms_and_aliases():
    cats = [{"name": "Бренды", "entries": [
        {"term": "Ювидерм", "aliases": ["juvederm"], "description": "филлер"}]}]
    g = format_glossary(cats)
    assert "Ювидерм" in g and "juvederm" in g and "филлер" in g


def test_glossary_token_cap_truncates_with_marker():
    cats = [{"name": "C", "entries": [
        {"term": f"термин-{i}", "aliases": [], "description": "x" * 50} for i in range(500)]}]
    g = format_glossary(cats, max_tokens=200)
    assert "опущено" in g
    assert len(g) // 4 <= 260  # ~max_tokens + marker slack


def test_structured_path_injects_glossary(monkeypatch):
    import tasks.quality as q
    captured = {}
    monkeypatch.setattr(q, "structured_completion",
                        lambda **kw: captured.update(kw) or {"score_version": 2})
    q._assess_with_structured_output("TRANSCRIPT", "[]", None, "",
                                     glossary="## Глоссарий\n- X")
    assert "## Глоссарий" in captured["user"]


def test_structured_path_injects_glossary_extended(monkeypatch):
    import tasks.quality as q
    captured = {}
    monkeypatch.setattr(q, "structured_completion",
                        lambda **kw: captured.update(kw) or {"score_version": 4})
    q._assess_with_structured_output("T", "[]", None, "",
                                     use_extended_schema=True,
                                     glossary="## Глоссарий\n- Y")
    assert "## Глоссарий" in captured["user"]

