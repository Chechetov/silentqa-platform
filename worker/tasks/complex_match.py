"""Match-or-create complex profile + aggregation.

Two layers:
- Pure functions (`merge_extractions`, `_merge_field`, `_dedup_strings`) — used by tests
  and re-used by `_recompute_aggregate` to mint the aggregated profile blob.
- DB-bound (`match_or_create_complex`, `_recompute_aggregate`) — invoked from the Celery
  extraction task and from backend complexes endpoints (manual merge/relink).
"""
from __future__ import annotations

from typing import Any

from tasks._text_normalize import _basic_normalize


def _is_empty_scalar(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _dedup_strings(values: list[str]) -> list[str]:
    """Order-preserving dedup with case-insensitive normalized comparison.

    When the same string appears multiple times with different casing, we keep
    the most canonical form: prefer mixed-case over all-lowercase, breaking
    ties by first-seen order (newest extraction first).
    """
    # Pass 1: for each normalised key, pick the best original form.
    # "Best" = not all-lowercase wins; among equals the first-seen wins.
    best: dict[str, str] = {}  # norm_key -> best original
    order: list[str] = []  # insertion order of norm keys
    for v in values:
        if not isinstance(v, str):
            continue
        key = _basic_normalize(v)
        if key not in best:
            best[key] = v
            order.append(key)
        else:
            # Prefer a form that has at least one uppercase letter
            current = best[key]
            if current == current.lower() and v != v.lower():
                best[key] = v
    return [best[k] for k in order]


def _merge_field(newest_first: list[Any]) -> Any:
    """Merge a single field's values from extractions (newest first).

    Scalars: first non-empty wins.
    Lists of strings: union + dedup, preserving the newest extraction's order.
    Dicts: recursive merge by key.
    Lists of dicts: union without dedup (dedup is only meaningful for strings).
    """
    if not newest_first:
        return None

    sample = next((v for v in newest_first if v is not None), None)
    if sample is None:
        return None

    if isinstance(sample, dict):
        keys: list[str] = []
        for v in newest_first:
            if isinstance(v, dict):
                for k in v.keys():
                    if k not in keys:
                        keys.append(k)
        return {k: _merge_field([v.get(k) for v in newest_first if isinstance(v, dict)]) for k in keys}

    if isinstance(sample, list):
        # decide: list of strings vs list of dicts
        flat: list[Any] = []
        for v in newest_first:
            if isinstance(v, list):
                flat.extend(v)
        if all(isinstance(x, str) for x in flat):
            return _dedup_strings(flat)
        return flat  # list of dicts: keep all without dedup

    # scalar
    for v in newest_first:
        if not _is_empty_scalar(v):
            return v
    return None


def merge_extractions(extractions_newest_first: list[dict]) -> dict:
    """Build aggregated profile from a list of extractions sorted newest first.

    Each extraction is a dict with keys: raw_data, created_at, session_id.
    Special handling for `additional_info` — wrapped with attribution, no dedup.
    """
    if not extractions_newest_first:
        return {}

    # All keys across all raw_data
    all_keys: list[str] = []
    for e in extractions_newest_first:
        for k in (e.get("raw_data") or {}).keys():
            if k not in all_keys:
                all_keys.append(k)

    out: dict = {}
    for k in all_keys:
        if k == "additional_info":
            notes: list[dict] = []
            # collect oldest-first so reading order is chronological
            for e in reversed(extractions_newest_first):
                values = (e.get("raw_data") or {}).get("additional_info") or []
                ts = e.get("created_at")
                ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts) if ts is not None else None
                for note in values:
                    if not isinstance(note, str) or not note.strip():
                        continue
                    notes.append({
                        "note": note,
                        "session_id": e.get("session_id"),
                        "recorded_at": ts_str,
                    })
            out["additional_info"] = notes
        else:
            values = [(e.get("raw_data") or {}).get(k) for e in extractions_newest_first]
            out[k] = _merge_field(values)
    return out


import json as _json
import uuid as _uuid

from sqlalchemy import text as _text
from sqlalchemy.orm import Session as _DbSession

from tasks._text_normalize import normalize_complex_name, normalize_developer


def match_or_create_complex(db: _DbSession, extraction_id) -> _uuid.UUID | None:
    """Find or create a complex profile for the given extraction; recompute its aggregate.

    Returns the complex_id (uuid.UUID), or None if the extraction lacks a usable
    name or developer. Mutates: complex_extractions.complex_id, complexes row.
    """
    row = db.execute(_text("SELECT raw_data FROM complex_extractions WHERE id=:id"),
                     {"id": extraction_id}).first()
    if not row:
        raise ValueError(f"extraction {extraction_id} not found")
    raw = row[0]
    name_raw = (raw or {}).get("name")
    developer_raw = (raw or {}).get("developer")
    name_norm = normalize_complex_name(name_raw)
    developer_norm = normalize_developer(developer_raw)
    if not name_norm or not developer_norm:
        return None

    existing = db.execute(_text("""
        SELECT id FROM complexes
        WHERE name_normalized=:n AND developer_normalized=:d
    """), {"n": name_norm, "d": developer_norm}).first()

    if existing:
        complex_id = existing[0]
    else:
        complex_id = _uuid.uuid4()
        db.execute(_text("""
            INSERT INTO complexes (id, name, name_normalized, developer, developer_normalized,
                                   class, district, aggregated_data)
            VALUES (:id, :name, :nn, :dev, :dn, :cls, :dist, '{}'::jsonb)
        """), {
            "id": complex_id,
            "name": name_raw,
            "nn": name_norm,
            "dev": developer_raw,
            "dn": developer_norm,
            "cls": (raw or {}).get("class"),
            "dist": ((raw or {}).get("location") or {}).get("district"),
        })

    db.execute(_text("UPDATE complex_extractions SET complex_id=:cid WHERE id=:id"),
               {"cid": complex_id, "id": extraction_id})

    _recompute_aggregate(db, complex_id)
    return complex_id


def _recompute_aggregate(db: _DbSession, complex_id) -> None:
    """Rebuild aggregated_data + indexed columns from all extractions of this complex."""
    rows = db.execute(_text("""
        SELECT raw_data, created_at, session_id
        FROM complex_extractions
        WHERE complex_id=:cid
        ORDER BY created_at DESC
    """), {"cid": complex_id}).all()

    extractions = [
        {"raw_data": r[0], "created_at": r[1], "session_id": str(r[2])}
        for r in rows
    ]
    aggregated = merge_extractions(extractions)

    name = aggregated.get("name")
    developer = aggregated.get("developer")
    cls = aggregated.get("class")
    loc = aggregated.get("location")
    district = loc.get("district") if isinstance(loc, dict) else None

    db.execute(_text("""
        UPDATE complexes SET
            aggregated_data = CAST(:agg AS jsonb),
            name = COALESCE(:name, name),
            name_normalized = COALESCE(:nn, name_normalized),
            developer = :dev,
            developer_normalized = COALESCE(:dn, developer_normalized),
            class = :cls,
            district = :dist,
            updated_at = now()
        WHERE id = :cid
    """), {
        "agg": _json.dumps(aggregated, ensure_ascii=False, default=str),
        "name": name,
        "nn": normalize_complex_name(name) if name else None,
        "dev": developer,
        "dn": normalize_developer(developer) if developer else None,
        "cls": cls,
        "dist": district,
        "cid": complex_id,
    })
