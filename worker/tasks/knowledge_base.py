"""Knowledge Base: transcript normalization, LLM glossary, ASR keyterms, tagging.

Pure functions operate on plain inputs (unit-tested, no DB). The kb_* DB wrappers
(below) open their own tenant connection and degrade to empty on ANY error so they
NEVER fail transcription (the pipeline marks the session failed + re-raises otherwise).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

KEYTERMS_LIMIT = 1000
ALIAS_LIMIT = 10000
GLOSSARY_MAX_TOKENS = 2000


class Matcher:
    """Compiled alternation over (alias → (term, entry_id)), longest-alias-first."""

    def __init__(self, pattern, lookup):
        self._pattern = pattern
        self._lookup = lookup  # casefolded alias -> (term, entry_id)

    def find_and_replace(self, text_str: str):
        hits: dict[str, int] = {}
        if self._pattern is None:
            return text_str, hits

        def _sub(mobj):
            alias = mobj.group(0)
            term, entry_id = self._lookup[alias.casefold()]
            hits[entry_id] = hits.get(entry_id, 0) + 1
            return term

        return self._pattern.sub(_sub, text_str), hits


def build_matcher(entries: list[dict]) -> Matcher:
    """entries: [{entry_id, term, aliases:[...]}]. Match term + aliases, case-insensitive,
    on word boundaries. Idempotent: the canonical `term` is itself an alias, so a second
    pass is a no-op. Longest alias first so multiword wins over a contained single word."""
    lookup: dict[str, tuple[str, str]] = {}
    for e in entries:
        forms = [e["term"], *(e.get("aliases") or [])]
        for f in forms:
            f = (f or "").strip()
            if f:
                lookup.setdefault(f.casefold(), (e["term"], e["entry_id"]))
    if not lookup:
        return Matcher(None, {})
    alts = sorted(lookup.keys(), key=len, reverse=True)[:ALIAS_LIMIT]
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(a) for a in alts) + r")(?!\w)",
        re.IGNORECASE,
    )
    return Matcher(pattern, lookup)


def normalize_transcript(transcript: list[dict], matcher: Matcher):
    """Rewrite each segment['text'] to canonical terms (LLM + saved transcript read it).
    Preserve the original in segment['orig_text']. Returns (transcript, hits=[(entry_id,count)])."""
    total: dict[str, int] = {}
    for seg in transcript:
        original = seg.get("text") or ""
        if not original:
            continue
        new_text, hits = matcher.find_and_replace(original)
        if new_text != original:
            seg.setdefault("orig_text", original)
            seg["text"] = new_text
        for entry_id, c in hits.items():
            total[entry_id] = total.get(entry_id, 0) + c
    return transcript, list(total.items())


def cap_keyterms(terms: list[str], limit: int = KEYTERMS_LIMIT) -> list[str]:
    seen, out = set(), []
    for t in terms:
        t = (t or "").strip()
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
        if len(out) >= limit:
            logger.info("kb keyterms capped at %d (dropped extras)", limit)
            break
    return out


def _approx_tokens(s: str) -> int:
    return len(s) // 4


def format_glossary(categories: list[dict], max_tokens: int = GLOSSARY_MAX_TOKENS) -> str:
    """categories: [{name, entries:[{term, aliases, description}]}] (feeds_llm only)."""
    if not categories:
        return ""
    lines = ["## Глоссарий (правильные написания и значения терминов)"]
    omitted = 0
    for cat in categories:
        for e in cat.get("entries") or []:
            alias_s = ", ".join(e.get("aliases") or [])
            desc = e.get("description") or ""
            line = f"- {e['term']}" + (f" (={alias_s})" if alias_s else "") + (f" — {desc}" if desc else "")
            candidate = "\n".join(lines + [line])
            if _approx_tokens(candidate) > max_tokens:
                omitted += 1
                continue
            lines.append(line)
    if omitted:
        lines.append(f"… ещё {omitted} записей опущено")
    return "\n".join(lines)
