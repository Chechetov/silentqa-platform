"""Backend-сторона: допустимые scenario id из company-config тенанта.

company-config — файлы COMPANIES_PATH/<id>.json (см. CLAUDE.md, routes/companies.py).
company_config_id тенанта — из shared.tenants. Возвращаем множество валидных id, чтобы
finish_session мог санкционировать клиентский appointment_type → scenario_id."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

# ВАЖНО: backend `settings` НЕ содержит COMPANIES_PATH (проверено: backend/app/config.py).
# Worker берёт его из env (worker/tasks/company_config.py); routes/companies.py — тоже из env.
# Читаем env напрямую — единый источник с воркером, дефолт /companies (как в CLAUDE.md / docker-mount).
def _companies_root() -> Path:
    return Path(os.getenv("COMPANIES_PATH", "/companies"))


@lru_cache(maxsize=64)
def _scenarios_for_config(config_id: str) -> frozenset[str]:
    path = _companies_root() / f"{config_id}.json"
    if not path.exists():
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(s["id"] for s in data.get("scenarios", []) if s.get("id"))


def valid_scenario(config_id: str | None, scenario_id: str | None) -> str | None:
    """Вернуть scenario_id, если он валиден для config_id; иначе None."""
    if not config_id or not scenario_id:
        return None
    return scenario_id if scenario_id in _scenarios_for_config(config_id) else None
