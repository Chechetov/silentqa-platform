import pytest

from tasks._text_normalize import normalize_complex_name, normalize_developer


@pytest.mark.parametrize("raw,expected", [
    ("Шагал", "шагал"),
    ("ЖК Шагал", "шагал"),
    ("ЖК «Шагал»", "шагал"),
    ('ЖК "Шагал"', "шагал"),
    ("жилой комплекс Шагал", "шагал"),
    ("Жилой Комплекс Шагал-2", "шагал 2"),
    ("МФК Шагалёв", "шагалев"),
    ("  Шагал   ", "шагал"),
    ("Апарт-комплекс Шагал", "шагал"),
    ("ЖК «Шагал».", "шагал"),
    ("Жилой район «Шагал»", "шагал"),
    ("ЖК “Шагал”", "шагал"),   # U+201C / U+201D curly double quotes
    ("ЖК ‘Шагал’", "шагал"),   # U+2018 / U+2019 curly single quotes
    ("ЖК", ""),
    ("", ""),
    (None, ""),
])
def test_normalize_complex_name(raw, expected):
    assert normalize_complex_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Эталон", "эталон"),
    ("ГК Эталон", "эталон"),
    ('Группа компаний "Эталон"', "эталон"),
    ("ООО Эталон", "эталон"),
    ("PIK", "pik"),
    ("Самолёт", "самолет"),
    ("", ""),
    (None, ""),
])
def test_normalize_developer(raw, expected):
    assert normalize_developer(raw) == expected
