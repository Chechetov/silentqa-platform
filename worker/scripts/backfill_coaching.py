"""Бэкфилл coaching.json (F-5) для уже обработанных звонков.

Кандидаты — сессии тенанта, у которых на диске есть `quality.json` с
`overall_score` не None И `transcript.json`, но НЕТ `coaching.json`. Для каждого
кандидата в режиме --apply вызывается LLM-коучинг и результат ложится в
`coaching.json`. Dry-run по умолчанию (только считает кандидатов).

⚠️ ВНИМАНИЕ: --apply ЖЖЁТ РЕАЛЬНЫЕ LLM-ВЫЗОВЫ (по одному на кандидата, деньги).
Сначала прогони без флага (dry-run) и посмотри, сколько звонков будет обработано.

  python scripts/backfill_coaching.py --tenant acme          # dry-run
  python scripts/backfill_coaching.py --all --apply
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
from tenancy.identifiers import schema_for_slug
from tenancy.registry import iter_active_tenants

from tasks.coaching import generate_coaching  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_coaching")

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


def backfill_tenant(slug: str, results_root, apply: bool = False) -> dict:
    stats = {"candidates": 0, "generated": 0, "skipped": 0}
    tenant_dir = Path(results_root) / slug
    if not tenant_dir.is_dir():
        logger.info(f"[{slug}] нет каталога результатов: {tenant_dir}")
        return stats
    for session_dir in sorted(p for p in tenant_dir.iterdir() if p.is_dir()):
        if (session_dir / "coaching.json").exists():
            continue  # уже есть коучинг
        quality = _load(session_dir, "quality.json")
        if not quality or quality.get("overall_score") is None:
            continue  # нет оценённого отчёта
        transcript = _load(session_dir, "transcript.json")
        if not transcript:
            continue  # нет транскрипта
        stats["candidates"] += 1
        if not apply:
            continue
        try:
            coaching = generate_coaching(transcript, quality)
        except Exception:
            logger.exception(f"[{slug}] {session_dir.name}: сбой генерации — скип")
            stats["skipped"] += 1
            continue
        if not coaching:
            stats["skipped"] += 1
            continue
        (session_dir / "coaching.json").write_text(
            json.dumps(coaching, ensure_ascii=False, indent=2), encoding="utf-8")
        stats["generated"] += 1
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tenant", help="слаг одного тенанта")
    g.add_argument("--all", action="store_true", help="все активные тенанты")
    ap.add_argument("--apply", action="store_true",
                    help="реально генерить и писать (без флага — dry-run)")
    args = ap.parse_args()

    slugs = ([args.tenant] if args.tenant
             else [t["slug"] for t in iter_active_tenants()])
    mode = "APPLY" if args.apply else "DRY-RUN"
    total = {"candidates": 0, "generated": 0, "skipped": 0}
    for slug in slugs:
        token = set_tenant_schema(schema_for_slug(slug))
        try:
            stats = backfill_tenant(slug, RESULTS_ROOT, apply=args.apply)
        finally:
            reset_tenant_schema(token)
        logger.info(f"[{mode}] {slug}: кандидатов {stats['candidates']}, "
                    f"сгенерено {stats['generated']}, скип {stats['skipped']}")
        for k in total:
            total[k] += stats[k]
    logger.info(f"[{mode}] ИТОГО: {total}")
    if not args.apply and total["candidates"]:
        logger.info(f"⚠️  --apply сожжёт ~{total['candidates']} реальных LLM-вызовов "
                    f"(по одному на кандидата). Повтори с --apply для записи.")


if __name__ == "__main__":
    main()
