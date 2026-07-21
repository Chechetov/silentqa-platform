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
def _scenario_list_for_config(config_id: str) -> tuple[tuple[str, str, bool], ...]:
    """Сценарии конфига как кортеж (id, name, classify) — hashable для lru_cache.

    classify=True, если у сценария есть `classify.hint` (кандидат для авто-типа
    созвона; фронт показывает по таким селектор «Тип созвона»). name → id, если
    не задан. Толерантен к отсутствию файла / битому JSON → ().
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
            classify = bool((s.get("classify") or {}).get("hint"))
            out.append((sid, s.get("name") or sid, classify))
    return tuple(out)


@lru_cache(maxsize=64)
def _scenarios_for_config(config_id: str) -> frozenset[str]:
    return frozenset(sid for sid, _name, _classify in _scenario_list_for_config(config_id))


@lru_cache(maxsize=64)
def _amocrm_subdomain_for_config(config_id: str) -> str | None:
    """Субдомен AmoCRM из company-config (ключ `amocrm_subdomain`), или None.

    Толерантен к отсутствию файла / битому JSON / пустому значению → None."""
    path = _companies_root() / f"{config_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sub = data.get("amocrm_subdomain")
    return sub.strip() if isinstance(sub, str) and sub.strip() else None


@lru_cache(maxsize=64)
def _card_label_for_config(config_id: str) -> str | None:
    """Заголовок структурированной карточки из company-config (`card_extraction.label`).

    Домен-специфика (дентал «Карта приёма» vs «Итоги созвона») живёт в конфиге, а не
    хардкодится во фронте. Толерантен к отсутствию файла / битому JSON / пустому → None."""
    path = _companies_root() / f"{config_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    label = (data.get("card_extraction") or {}).get("label")
    return label.strip() if isinstance(label, str) and label.strip() else None


def clear_scenario_caches() -> None:
    """Invalidate all scenario caches. Call after any company-config write.

    Единая точка инвалидации: чистит КАЖДЫЙ config-кеш модуля. Если добавляешь
    ещё один lru_cache по company-config — сбрасывай его здесь же, иначе сайт записи
    конфига (routes/companies.py:_write_config) снова рассинхронизируется."""
    _scenario_list_for_config.cache_clear()
    _scenarios_for_config.cache_clear()
    _amocrm_subdomain_for_config.cache_clear()
    _card_label_for_config.cache_clear()


def scenarios_for(config_id: str | None) -> list[dict]:
    """Сценарии конфига как [{"id":..., "name":...}] (name → id, если отсутствует).

    Для рендеринга списка типов приёмов/звонков в рекордере (через /features)."""
    if not config_id:
        return []
    return [{"id": sid, "name": name, "classify": classify}
            for sid, name, classify in _scenario_list_for_config(config_id)]


def valid_scenario(config_id: str | None, scenario_id: str | None) -> str | None:
    """Вернуть scenario_id, если он валиден для config_id; иначе None."""
    if not config_id or not scenario_id:
        return None
    return scenario_id if scenario_id in _scenarios_for_config(config_id) else None


def amocrm_subdomain_for(config_id: str | None) -> str | None:
    """Субдомен AmoCRM тенанта из company-config — для deep-link в дашборде через
    /features (гейтится модулем amocrm). None, если конфиг/ключ отсутствует."""
    if not config_id:
        return None
    return _amocrm_subdomain_for_config(config_id)


def card_label_for(config_id: str | None) -> str | None:
    """Заголовок карточки тенанта (`card_extraction.label`) — для рендера карточки в
    дашборде через /features. None, если конфиг/блок/label отсутствует."""
    if not config_id:
        return None
    return _card_label_for_config(config_id)
