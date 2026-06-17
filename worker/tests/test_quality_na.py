from tasks.quality import QUALITY_JSON_SCHEMA, QUALITY_JSON_SCHEMA_V3


def _score_type(schema):
    return schema["schema"]["properties"]["criteria"]["items"]["properties"]["score"]["type"]


def test_v2_score_allows_null():
    assert _score_type(QUALITY_JSON_SCHEMA) == ["integer", "null"]


def test_v4_path_score_stays_integer_only():
    # realestate uses V4 (inherits V3 criteria) — must NOT gain null (regression guard)
    assert _score_type(QUALITY_JSON_SCHEMA_V3) == "integer"
