"""Активный профиль оценки сценария (пер-тенант, из БД тенанта).

Переопределяет критерии/промпт оценки из файл-конфига в шаге quality.
Sync-чтение worker-стороны — паттерн company_config.tenant_company_config_id().
"""
from __future__ import annotations

import json
import logging

from tenancy.db import get_sync_db_url, tenant_connect

logger = logging.getLogger(__name__)


def load_active_eval_profile(scenario_id: str | None) -> dict | None:
    """Вернуть {'criteria': list, 'prompt': str|None} активной версии профиля
    для сценария, либо None (не настроен / БД недоступна / таблицы ещё нет)."""
    if not scenario_id or not get_sync_db_url():
        return None
    try:
        conn = tenant_connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT criteria, prompt FROM evaluation_profile_versions "
                    "WHERE scenario_id = %s AND is_active = true LIMIT 1",
                    (scenario_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        # Таблица ещё не мигрирована / транзиентный сбой — мягкий fallback на файл-конфиг.
        logger.exception("load_active_eval_profile failed; falling back to file config")
        return None

    if not row:
        return None
    criteria, prompt = row
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except Exception:
            criteria = []
    return {"criteria": criteria or [], "prompt": prompt}
