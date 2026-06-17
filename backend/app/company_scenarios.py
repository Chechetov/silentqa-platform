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
def _scenario_list_for_config(config_id: str) -> tuple[tuple[str, str], ...]:
    """Сценарии конфига как кортеж (id, name) — hashable для lru_cache.

    name → id, если в конфиге не задан. Толерантен к отсутствию файла / битому JSON → ().
    """
    path = _companies_root() / f"{config_id}.json"
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    out = []
    for s in data.get("scenarios", []):
        sid = s.get("id")
        if sid:
            out.append((sid, s.get("name") or sid))
    return tuple(out)


@lru_cache(maxsize=64)
def _scenarios_for_config(config_id: str) -> frozenset[str]:
    return frozenset(sid for sid, _ in _scenario_list_for_config(config_id))


def scenarios_for(config_id: str | None) -> list[dict]:
    """Сценарии конфига как [{"id":..., "name":...}] (name → id, если отсутствует).

    Для рендеринга списка типов приёмов/звонков в рекордере (через /features)."""
    if not config_id:
        return []
    return [{"id": sid, "name": name} for sid, name in _scenario_list_for_config(config_id)]


def valid_scenario(config_id: str | None, scenario_id: str | None) -> str | None:
    """Вернуть scenario_id, если он валиден для config_id; иначе None."""
    if not config_id or not scenario_id:
        return None
    return scenario_id if scenario_id in _scenarios_for_config(config_id) else None
