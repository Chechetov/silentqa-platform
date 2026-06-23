# B3 — безопасная чистка Zoom-ЖК шаблона из не-complexes тенантов

**Дата:** 2026-06-23
**Контекст:** хвост `universal-knowledge-base` (де-realestate-ификация за module-флагами). Спека-родитель: `2026-06-16-universal-knowledge-base-design.md` §9 (явно откладывает этот пункт). Статус: **дизайн на утверждение, НЕ применять без ревью пользователя**.

## Проблема

Миграция `backend/alembic/versions/008_seed_zoom_meeting_template.py` сидит RE-специфичный evaluation-шаблон **«Zoom-встреча брокера (презентация ЖК)»** (kind=`evaluation`, `ON CONFLICT (name) DO NOTHING`) в `extraction_templates`. Тенант-трек миграции **фанится на ВСЕ схемы** через `python -m app.migrate`, поэтому шаблон оказался в схеме каждого тенанта — включая не-RE (fulldent, chechetov), где «презентация ЖК» бессмысленна и засоряет редактор шаблонов / выпадашку «Переоценить с другим шаблоном».

B2 уже убрал АВТО-применение этого шаблона к десктоп-записям не-complexes тенантов (гейт по модулю). Но сама строка-шаблон в их схемах остаётся и может быть выбрана вручную.

## Почему отложено / в чём риск

- **Миграция 008 уже применена в проде** во все схемы. Править её `upgrade()` ретроактивно нельзя — на застемпленных схемах она не перезапустится.
- Прямой `DELETE` в миграции опасен: тенант-миграция фанится на ВСЕ схемы, включая realestate, который шаблон должен сохранить; и мог бы удалить шаблон, на который ссылаются существующие сессии.
- Прод-данные: удаление должно быть идемпотентным, с проверкой «не используется», dry-run по умолчанию.

## Цели / не-цели

**Цели:**
1. Удалить Zoom-ЖК шаблон из схем тенантов **без модуля complexes**, но только если он **не используется** (ни одна сессия на него не ссылается).
2. Прекратить пере-сидинг этого шаблона для **будущих** не-complexes тенантов при провижининге.
3. realestate и любой тенант с `complexes:true` — **не затронуты**.

**Не-цели:** удаление complexes-стека (`complexes`/`extraction_templates` как механизм) — он остаётся, гейтится модулем. Чистка прочих RE-строк — отдельно.

## Решение (две части)

### Часть 1 — одноразовый ops-скрипт чистки существующих данных

**НЕ миграция** (та фанится на все схемы и не видит модули per-tenant). Скрипт по образцу `worker/scripts/seed_realestate_kb.py`: итерирует тенантов, проверяет модуль + использование, dry-run по умолчанию.

Файл: `worker/scripts/cleanup_zoom_template.py`

```python
"""Одноразовая чистка: убрать RE-шаблон «Zoom-встреча брокера (презентация ЖК)»
из схем тенантов БЕЗ модуля complexes, только если он не используется сессиями.

Идемпотентно. По умолчанию DRY-RUN; реальное удаление — только с --apply.
Запуск:  cd worker && python -m scripts.cleanup_zoom_template [--apply]
"""
import sys

from tenancy.context import reset_tenant_schema, set_tenant_schema
from tenancy.db import tenant_connect
from tenancy.registry import iter_active_tenants

ZOOM_NAME = "Zoom-встреча брокера (презентация ЖК)"
# Дефолты модулей — как в backend/app/modules.py (complexes по умолчанию OFF).
# Инлайним, чтобы не тянуть app.* в worker (tenancy/ app-independent).
_COMPLEXES_DEFAULT = False


def _complexes_on(modules: dict | None) -> bool:
    m = modules or {}
    return bool(m["complexes"]) if "complexes" in m else _COMPLEXES_DEFAULT


def cleanup(apply: bool) -> None:
    deleted = skipped_inuse = not_found = kept_complexes = 0
    for t in iter_active_tenants():
        slug, schema, modules = t["slug"], t["schema_name"], t.get("modules")
        if _complexes_on(modules):
            kept_complexes += 1
            continue  # complexes-тенант (incl. realestate) — шаблон легитимен
        token = set_tenant_schema(schema)
        try:
            conn = tenant_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM extraction_templates WHERE name = %s", (ZOOM_NAME,))
                    row = cur.fetchone()
                    if not row:
                        not_found += 1
                        continue
                    tpl_id = str(row[0])
                    # «Используется» = есть сессия с этим template_id в metadata.
                    cur.execute(
                        "SELECT count(*) FROM sessions WHERE metadata->>'template_id' = %s",
                        (tpl_id,),
                    )
                    used = cur.fetchone()[0]
                    if used:
                        skipped_inuse += 1
                        print(f"  SKIP {slug}: шаблон используется {used} сессиями — оставляю")
                        continue
                    if apply:
                        cur.execute("DELETE FROM extraction_templates WHERE id = %s", (tpl_id,))
                        conn.commit()
                        deleted += 1
                        print(f"  DEL  {slug}: Zoom-ЖК шаблон удалён ({tpl_id})")
                    else:
                        deleted += 1
                        print(f"  DRY  {slug}: удалил бы Zoom-ЖК шаблон ({tpl_id})")
            finally:
                conn.close()
        finally:
            reset_tenant_schema(token)
    mode = "ПРИМЕНЕНО" if apply else "DRY-RUN (ничего не удалено)"
    print(f"\n[{mode}] удалено/к удалению={deleted}, в использовании(оставлено)={skipped_inuse}, "
          f"нет шаблона={not_found}, complexes-тенантов(пропущено)={kept_complexes}")


if __name__ == "__main__":
    cleanup(apply="--apply" in sys.argv)
```

**Свойства безопасности:**
- DRY-RUN по умолчанию → оператор сначала видит, что будет удалено.
- Удаляет **только** у тенантов без complexes (realestate/complexes-тенанты пропускаются явно).
- Удаляет **только** неиспользуемый шаблон (проверка `sessions.metadata->>'template_id'`).
- Идемпотентно (повторный прогон → «нет шаблона»).
- Через `tenant_connect()` (search_path забит в libpq options) — изоляция тенанта; raw `psycopg2.connect` запрещён тестом.

### Часть 2 — прекратить пере-сидинг для будущих тенантов

Правка `008` безопасна для существующих схем: они застемплены за 008 (alembic_version per-schema на head 013+), 008 на них НЕ перезапускается; Alembic не чексуммит тело миграции. Edit влияет **только на новые схемы** (provision → run_tenant прогоняет 001→head). На момент 008 строка `shared.tenants` для нового тенанта уже есть (provision вставляет её до run_tenant), поэтому `current_schema()`-lookup модулей валиден.

Правка `upgrade()` в 008 — сидить только если у тенанта включён complexes:

```python
def upgrade() -> None:
    op.execute(f"""INSERT INTO extraction_templates (name, description, kind, prompt, json_schema)
        SELECT
            'Zoom-встреча брокера (презентация ЖК)',
            'Оценка живой Zoom-встречи брокера ...',  -- без изменений
            'evaluation',
            $${FULL_PROMPT}$$,
            $${json.dumps(SCHEMA, ensure_ascii=False)}$$::jsonb
        WHERE EXISTS (
            SELECT 1 FROM shared.tenants
            WHERE schema_name = current_schema()
              AND COALESCE((modules->>'complexes')::boolean, false) = true
        )
        ON CONFLICT (name) DO NOTHING""")
```

- Существующие тенанты: 008 не перезапускается → без изменений.
- Новый complexes-тенант: получит шаблон.
- Новый не-complexes тенант: НЕ получит (цель).
- `downgrade()` без изменений (DELETE by name — безопасен, no-op если строки нет).

**Альтернатива (если править применённую миграцию не хочется):** оставить 008 как есть и гонять Часть-1 скрипт после каждого провижининга. Минус — операционно хрупко (легко забыть). Рекомендую правку 008 (durable).

## Краевые случаи
- **Тенант включает complexes ПОСЛЕ провижининга:** 008 уже прошла, шаблона нет. Решение: при включении модуля complexes (или вручную) досидить шаблон — можно добавить обратный режим в скрипт (`--seed-complexes`) или сидить из админки. Редкий кейс; вне MVP-чистки, отметить в follow-up.
- **Шаблон используется сессиями у не-complexes тенанта** (например, был авто-применён до B2): скрипт его НЕ удаляет (SKIP, лог). Оператор решает вручную (переоценить сессии другим шаблоном, затем удалить).

## Рунбук применения (прод)
1. Влить эту ветку (модульные гейты B1–F7) — уже в PR.
2. `cd /root/projects/silentqa/worker && python -m scripts.cleanup_zoom_template`  → **DRY-RUN**, изучить вывод (кого затронет).
3. Ревью списка с пользователем.
4. `... cleanup_zoom_template --apply`  → реальное удаление у не-complexes/неиспользующих.
5. Правку 008 (Часть 2) задеплоить обычным путём; она затронет только будущие провижининги (бэкенд-рестарт прогоняет миграции, но на застемпленных схемах 008 не перезапустится — окна «нет таблицы» нет).
6. Проверить: у fulldent/chechetov шаблона нет (`SELECT 1 FROM t_fulldent.extraction_templates WHERE name='Zoom-встреча брокера (презентация ЖК)'` → пусто); у realestate — есть.

## Тесты (TDD при реализации)
- `_complexes_on`: дефолт OFF; явный true/false.
- Скрипт: на фейковом реестре/конн — удаляет у не-complexes без сессий; SKIP при использующей сессии; пропускает complexes-тенанта; идемпотентность.
- Миграция 008 (pure SQL-shape, как `test_migration_*`): upgrade содержит `WHERE EXISTS ... modules->>'complexes'`; down_revision='007' не тронут.
- Регресс: realestate-сценарий не затронут.
