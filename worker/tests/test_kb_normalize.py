from tasks.knowledge_base import build_matcher, cap_keyterms, normalize_transcript

ENTRIES = [
    {"entry_id": "e1", "term": "Эталон", "aliases": ["эталон", "etalon"]},
    {"entry_id": "e2", "term": "ЖК Шагал", "aliases": ["шагал", "жк шагал"]},
]


def test_normalize_rewrites_segment_text_case_insensitive():
    m = build_matcher(ENTRIES)
    t = [{"speaker": "A", "text": "был у etalon вчера"}]
    out, hits = normalize_transcript(t, m)
    assert out[0]["text"] == "был у Эталон вчера"
    assert out[0]["orig_text"] == "был у etalon вчера"
    assert ("e1", 1) in hits


def test_normalize_word_boundaries_and_multiword():
    m = build_matcher(ENTRIES)
    t = [{"speaker": "A", "text": "смотрел жк шагал и эталоновый дом"}]
    out, hits = normalize_transcript(t, m)
    # multiword alias replaced; 'эталоновый' NOT matched (boundary)
    assert "ЖК Шагал" in out[0]["text"]
    assert "эталоновый" in out[0]["text"]
    assert dict(hits).get("e1") is None


def test_normalize_is_idempotent():
    m = build_matcher(ENTRIES)
    t = [{"speaker": "A", "text": "у etalon"}]
    once, _ = normalize_transcript(t, m)
    twice, _ = normalize_transcript([dict(s) for s in once], m)
    assert twice[0]["text"] == once[0]["text"] == "у Эталон"


def test_cap_keyterms_dedup_and_limit():
    terms = ["a", "a", "b"] + [f"t{i}" for i in range(2000)]
    out = cap_keyterms(terms, limit=1000)
    assert len(out) == 1000 and len(set(out)) == 1000
