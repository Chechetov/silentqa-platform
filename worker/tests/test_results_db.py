"""Чистые функции results_db: возражения (2 формата), talk-метрики, риск-флаги."""
from tasks.results_db import (
    RISK_DEFAULTS, compute_risk_flags, compute_talk_metrics,
    extract_objections, sentiment_counts_from,
)

# --- extract_objections ---

def test_objections_v4_quality_shape():
    qr = {"objections": [{"text": "Дорого", "category": "too_expensive",
                          "broker_response": "Рассрочка", "handling_quality": 7,
                          "resolved": True}]}
    out = extract_objections(qr, None)
    assert out == [{"text": "Дорого", "category": "too_expensive",
                    "resolved": True, "handling_quality": 7}]


def test_objections_card_shape_used_when_quality_empty():
    card = {"objections": [{"objection": "Подумаю", "raised_by": "клиент",
                            "handled": False, "handling": None, "improvement": "..."}]}
    out = extract_objections({"objections": []}, card)
    assert out == [{"text": "Подумаю", "category": "other",
                    "resolved": False, "handling_quality": None}]


def test_objections_quality_wins_over_card():
    qr = {"objections": [{"text": "A", "category": "no_time",
                          "broker_response": "", "handling_quality": 5, "resolved": False}]}
    card = {"objections": [{"objection": "B", "handled": True}]}
    assert [o["text"] for o in extract_objections(qr, card)] == ["A"]


def test_objections_empty_inputs():
    assert extract_objections(None, None) == []
    assert extract_objections({}, {}) == []


def test_objections_malformed_items_skipped():
    qr = {"objections": ["мусор", {"category": "other"}]}  # без text — скип
    assert extract_objections(qr, None) == []


# --- compute_talk_metrics ---

SEGS = [
    {"speaker": "A", "start": 0.0, "end": 10.0, "text": "…"},
    {"speaker": "B", "start": 10.0, "end": 14.0, "text": "…"},
    {"speaker": "A", "start": 14.0, "end": 20.0, "text": "…"},
    {"speaker": "A", "start": 20.0, "end": 25.0, "text": "…"},
]


def test_talk_with_roles():
    m = compute_talk_metrics(SEGS, {"A": "manager", "B": "client"})
    assert m["manager_sec"] == 21.0 and m["client_sec"] == 4.0
    assert abs(m["talk_ratio"] - 21.0 / 25.0) < 1e-9
    assert m["longest_monologue_sec"] == 11.0  # A: 14→25 непрерывно
    assert m["unattributed"] is False


def test_talk_without_roles_top2_heuristic():
    m = compute_talk_metrics(SEGS, None)
    # топ-2 по времени: A(21с) → manager-слот, B(4с) → client-слот, пометка unattributed
    assert m["manager_sec"] == 21.0 and m["client_sec"] == 4.0
    assert m["unattributed"] is True


def test_talk_third_speaker_goes_other():
    segs = SEGS + [{"speaker": "C", "start": 25.0, "end": 27.0, "text": "…"}]
    m = compute_talk_metrics(segs, {"A": "manager", "B": "client", "C": "other"})
    assert m["other_sec"] == 2.0


def test_talk_empty():
    assert compute_talk_metrics([], None) is None
    assert compute_talk_metrics(None, None) is None


# --- sentiment_counts_from ---

def test_sentiment_counts():
    res = [{"sentiment": "positive"}, {"sentiment": "negative"},
           {"sentiment": "negative"}, {"sentiment": "neutral"}]
    assert sentiment_counts_from(res) == {"positive": 1, "neutral": 1, "negative": 2}
    assert sentiment_counts_from(None) is None
    assert sentiment_counts_from([]) == {"positive": 0, "neutral": 0, "negative": 0}


# --- compute_risk_flags ---

def test_risk_low_score_default_threshold():
    assert "low_score" in compute_risk_flags(3, None, [], None)
    assert "low_score" not in compute_risk_flags(4, None, [], None)
    assert compute_risk_flags(None, None, [], None) == []  # нет скора — нет low_score


def test_risk_negative_sentiment_ratio():
    counts = {"positive": 1, "neutral": 1, "negative": 3}  # 0.6 > 0.4
    assert "negative_sentiment" in compute_risk_flags(8, counts, [], None)
    counts_ok = {"positive": 4, "neutral": 4, "negative": 2}  # 0.2
    assert "negative_sentiment" not in compute_risk_flags(8, counts_ok, [], None)


def test_risk_unresolved_objections():
    objs = [{"text": "Дорого", "category": "too_expensive",
             "resolved": False, "handling_quality": None}]
    assert "unresolved_objections" in compute_risk_flags(8, None, objs, None)
    assert "unresolved_objections" not in compute_risk_flags(
        8, None, [{**objs[0], "resolved": True}], None)


def test_risk_custom_thresholds():
    assert "low_score" in compute_risk_flags(6, None, [], {"risk_score_below": 7})
    assert RISK_DEFAULTS["risk_score_below"] == 4
