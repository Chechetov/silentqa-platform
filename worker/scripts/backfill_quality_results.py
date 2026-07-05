"""Бэкфилл quality_results из исторических JSON-файлов результатов.

Dry-run по умолчанию; --apply для записи. Идемпотентен (upsert) — безопасно
гонять повторно, в т.ч. после reprocess-потока (тот перезаписывает quality.json,
и повторный прогон доносит изменения до таблицы). NB: reassess_quality.py пишет
только quality_v2.json и не трогает quality.json, поэтому его результаты этот
бэкфилл НЕ подхватывает.

  python scripts/backfill_quality_results.py --tenant acme          # dry-run
  python scripts/backfill_quality_results.py --all --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # worker/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))          # repo root

from tenancy.context import reset_tenant_schema, set_tenant_schema
from tenancy.db import tenant_connect
from tenancy.identifiers import schema_for_slug
from tenancy.registry import iter_active_tenants

from tasks.results_db import record_quality_result  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill")

RESULTS_ROOT = os.getenv("RESULTS_STORAGE_PATH", "./data/results")


def _load(d: Path, name: str):
    f = d / name
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        logger.warning(f"  битый {f} — пропускаю файл")
        return None


def _session_exists(session_id: str) -> bool:
    conn = tenant_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sessions WHERE id = %s", (session_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _upsert_from_files(session_id: str, quality, transcript, sentiment, card) -> bool:
    # record_quality_result сам читает сессию, считает метрики и upsert'ит;
    # skip_reason достаём из самого отчёта (гейтовые отчёты его содержат).
    flags = record_quality_result(
        session_id, quality, card=card, transcript=transcript,
        sentiment_results=sentiment,
        skip_reason=(quality or {}).get("skip_reason"))
    return True  # best-effort: ошибки уже залогированы внутри


def backfill_tenant(slug: str, results_root, apply: bool = False) -> dict:
    stats = {"found": 0, "upserted": 0, "skipped": 0}
    tenant_dir = Path(results_root) / slug
    if not tenant_dir.is_dir():
        logger.info(f"[{slug}] нет каталога результатов: {tenant_dir}")
        return stats
    for session_dir in sorted(p for p in tenant_dir.iterdir() if p.is_dir()):
        quality = _load(session_dir, "quality.json")
        if quality is None:
            continue
        stats["found"] += 1
        sid = session_dir.name
        if not _session_exists(sid):
            logger.warning(f"[{slug}] {sid}: нет строки sessions — скип")
            stats["skipped"] += 1
            continue
        if not apply:
            continue
        _upsert_from_files(sid, quality,
                           _load(session_dir, "transcript.json"),
                           _load(session_dir, "sentiment.json"),
                           _load(session_dir, "card.json"))
        stats["upserted"] += 1
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tenant", help="слаг одного тенанта")
    g.add_argument("--all", action="store_true", help="все активные тенанты")
    ap.add_argument("--apply", action="store_true",
                    help="реально писать (без флага — dry-run)")
    args = ap.parse_args()

    slugs = ([args.tenant] if args.tenant
             else [t["slug"] for t in iter_active_tenants()])
    mode = "APPLY" if args.apply else "DRY-RUN"
    total = {"found": 0, "upserted": 0, "skipped": 0}
    for slug in slugs:
        token = set_tenant_schema(schema_for_slug(slug))
        try:
            stats = backfill_tenant(slug, RESULTS_ROOT, apply=args.apply)
        finally:
            reset_tenant_schema(token)
        logger.info(f"[{mode}] {slug}: найдено {stats['found']}, "
                    f"записано {stats['upserted']}, скип {stats['skipped']}")
        for k in total:
            total[k] += stats[k]
    logger.info(f"[{mode}] ИТОГО: {total}")
    if not args.apply and total["found"]:
        logger.info("Повтори с --apply для записи.")


if __name__ == "__main__":
    main()
