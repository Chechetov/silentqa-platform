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


def test_structured_path_injects_glossary():
    import tasks.quality as q
    captured = {}

    class FakeResp:
        output_text = '{"score_version": 2}'

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kw):
                captured.update(kw)
                return FakeResp()

    q._assess_with_structured_output(FakeClient(), "TRANSCRIPT", "[]", None, "",
                                     glossary="## Глоссарий\n- X")
    user_msg = [m for m in captured["input"] if m["role"] == "user"][0]["content"]
    assert "## Глоссарий" in user_msg


def test_legacy_path_injects_glossary():
    import tasks.quality as q
    captured = {}

    class FakeResp:
        choices = [type("C", (), {"message": type("M", (), {"content": "{}"})})()]

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return FakeResp()

    q._assess_with_legacy_prompt(FakeClient(), "T", "[]", None, "", glossary="## Глоссарий\n- Y")
    assert "## Глоссарий" in captured["messages"][0]["content"]

