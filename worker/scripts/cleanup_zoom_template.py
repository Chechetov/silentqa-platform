"""Одноразовая чистка (B3): убрать RE-шаблон «Zoom-встреча брокера (презентация ЖК)»
из схем тенантов БЕЗ модуля complexes, только если он не используется (ни сессией,
ни извлечением — зеркалит delete-guard backend/app/routes/templates.py).

Почему скрипт, а не миграция: тенант-миграция фанится на ВСЕ схемы и не видит
per-tenant модули. Скрипт итерирует реестр, проверяет модуль + использование.

Идемпотентно. По умолчанию DRY-RUN; реальное удаление — только с --apply.
Запуск:  cd worker && python -m scripts.cleanup_zoom_template [--apply]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy

from tenancy.context import reset_tenant_schema, set_tenant_schema  # noqa: E402
from tenancy.db import tenant_connect  # noqa: E402
from tenancy.registry import iter_active_tenants  # noqa: E402

ZOOM_NAME = "Zoom-встреча брокера (презентация ЖК)"
# Дефолт модуля complexes — как в backend/app/modules.py (OFF). Инлайним, чтобы
# не тянуть app.* в worker (tenancy/ остаётся app-independent).
_COMPLEXES_DEFAULT = False


def _complexes_on(modules: dict | None) -> bool:
    m = modules or {}
    return bool(m["complexes"]) if "complexes" in m else _COMPLEXES_DEFAULT


def cleanup(apply: bool) -> dict:
    counts = {"deleted": 0, "skipped_inuse": 0, "not_found": 0, "kept_complexes": 0}
    for t in iter_active_tenants():
        slug, schema, modules = t["slug"], t["schema_name"], t.get("modules")
        if _complexes_on(modules):
            counts["kept_complexes"] += 1
            continue  # complexes-тенант (incl. realestate) — шаблон легитимен
        token = set_tenant_schema(schema)
        try:
            conn = tenant_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM extraction_templates WHERE name = %s", (ZOOM_NAME,))
                    row = cur.fetchone()
                    if not row:
                        counts["not_found"] += 1
                        continue
                    tpl_id = str(row[0])
                    # «Используется» = ссылается извлечение ИЛИ сессия — тот же
                    # инвариант, что у delete-guard в routes/templates.py.
                    cur.execute(
                        "SELECT "
                        "(SELECT count(*) FROM complex_extractions WHERE template_id::text = %s), "
                        "(SELECT count(*) FROM sessions WHERE metadata->>'template_id' = %s)",
                        (tpl_id, tpl_id),
                    )
                    exts, sess = cur.fetchone()
                    if (exts or 0) + (sess or 0):
                        counts["skipped_inuse"] += 1
                        print(f"  SKIP {slug}: шаблон используется ({sess} сессий, {exts} извлечений) — оставляю")
                        continue
                    if apply:
                        cur.execute("DELETE FROM extraction_templates WHERE id = %s", (tpl_id,))
                        conn.commit()
                        counts["deleted"] += 1
                        print(f"  DEL  {slug}: Zoom-ЖК шаблон удалён ({tpl_id})")
                    else:
                        counts["deleted"] += 1
                        print(f"  DRY  {slug}: удалил бы Zoom-ЖК шаблон ({tpl_id})")
            finally:
                conn.close()
        finally:
            reset_tenant_schema(token)
    mode = "ПРИМЕНЕНО" if apply else "DRY-RUN (ничего не удалено)"
    print(f"\n[{mode}] удалено/к удалению={counts['deleted']}, "
          f"в использовании(оставлено)={counts['skipped_inuse']}, "
          f"нет шаблона={counts['not_found']}, complexes-тенантов(пропущено)={counts['kept_complexes']}")
    return counts


if __name__ == "__main__":
    cleanup(apply="--apply" in sys.argv)
