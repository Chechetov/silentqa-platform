"""Normalization helpers for matching complex/developer names across extractions."""
from __future__ import annotations

import re

# Order matters: longest prefix first so "жилой комплекс" matches before "жилой"
_COMPLEX_PREFIXES = (
    "апарт комплекс",
    "жилой комплекс",
    "жилой район",
    "мфк",
    "жк",
)
_DEVELOPER_PREFIXES = (
    "группа компаний",
    "гк",
    "ооо",
    "оао",
    "зао",
    "ао",
    "пао",
)
# Build from chr() — no literal curly quotes here, immune to smart-quote autocorrect.
_QUOTES = (
    "«"  # 0x00ab LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    "»"  # 0x00bb RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    '"'  # 0x0022 ASCII QUOTATION MARK
    "“"  # 0x201c LEFT DOUBLE QUOTATION MARK
    "”"  # 0x201d RIGHT DOUBLE QUOTATION MARK
    "‘"  # 0x2018 LEFT SINGLE QUOTATION MARK
    "’"  # 0x2019 RIGHT SINGLE QUOTATION MARK
    "‚"  # 0x201a SINGLE LOW-9 QUOTATION MARK
    "„"  # 0x201e DOUBLE LOW-9 QUOTATION MARK
)


def _basic_normalize(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = s.replace("ё", "е")
    # strip quotes
    for q in _QUOTES:
        s = s.replace(q, "")
    # punctuation/separators -> space
    s = re.sub(r"[\.,;:!\?\-_/\\]+", " ", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_prefix(s: str, prefixes: tuple[str, ...]) -> str:
    for p in prefixes:
        if s == p:
            return ""
        if s.startswith(p + " "):
            return s[len(p) + 1:].strip()
    return s


def normalize_complex_name(raw: str | None) -> str:
    s = _basic_normalize(raw)
    return _strip_prefix(s, _COMPLEX_PREFIXES)


def normalize_developer(raw: str | None) -> str:
    s = _basic_normalize(raw)
    return _strip_prefix(s, _DEVELOPER_PREFIXES)
