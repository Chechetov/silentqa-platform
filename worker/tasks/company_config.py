"""
Загрузка конфигурации компании.
Конфиги хранятся в /companies/{company_id}.json и монтируются в контейнер.
"""
import json
import logging
import os
from pathlib import Path

from tenancy.db import shared_connect

logger = logging.getLogger(__name__)

COMPANIES_DIR = Path(os.getenv("COMPANIES_PATH", "/companies"))

_cache: dict[str, dict] = {}

_TENANT_COMPANY_CACHE: dict[str, str | None] = {}


def tenant_company_config_id() -> str | None:
    """company_config_id текущего тенанта из shared.tenants (кеш на процесс).

    Worker перезапускается при деплоях/ротациях (см. CLAUDE.md), поэтому
    простой module-level кеш безопасен — как и остальные module-globals тут.
    """
    from tenancy.context import require_tenant_slug

    slug = require_tenant_slug()
    if slug in _TENANT_COMPANY_CACHE:
        return _TENANT_COMPANY_CACHE[slug]
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT company_config_id FROM shared.tenants WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    _TENANT_COMPANY_CACHE[slug] = row[0] if row else None
    return _TENANT_COMPANY_CACHE[slug]


def load_company_config(company_id: str | None) -> dict:
    """Load company config by ID. Falls back to 'default' if not found."""
    company_id = company_id or "default"

    if company_id in _cache:
        return _cache[company_id]

    config_path = COMPANIES_DIR / f"{company_id}.json"
    if not config_path.exists():
        logger.warning(f"Company config not found: {config_path}, using default")
        if company_id != "default":
            return load_company_config("default")
        return {"id": "default", "name": "Default", "asr": {"word_boost": []}, "quality": {}}

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    _cache[company_id] = config
    logger.info(f"Loaded company config: {company_id} ({config.get('name', '')})")
    return config


def get_word_boost(company_config: dict) -> list[str]:
    """Extract word_boost list from company config."""
    return company_config.get("asr", {}).get("word_boost", [])


def get_protocol(company_config: dict) -> str | None:
    """Extract quality assessment protocol from company config."""
    return company_config.get("quality", {}).get("protocol")


def get_custom_prompt(company_config: dict) -> str | None:
    """Extract custom LLM prompt from company config."""
    return company_config.get("quality", {}).get("prompt")


def get_asr_engine(company_config: dict) -> str | None:
    """Get per-company ASR engine override (or None for global default)."""
    return company_config.get("asr", {}).get("engine")


def get_scenarios(company_config: dict) -> list[dict]:
    """Get list of evaluation scenarios for a company."""
    return company_config.get("scenarios", [])


def get_classifiable_scenarios(company_config: dict) -> list[dict]:
    """Сценарии с подсказкой `classify.hint` — кандидаты для авто-классификации
    типа созвона. Меньше двух → авто-классификация не запускается."""
    return [s for s in get_scenarios(company_config) if (s.get("classify") or {}).get("hint")]


def get_scenario(company_config: dict, scenario_id: str | None) -> dict | None:
    """Find a specific scenario by ID. Returns None if not found."""
    if not scenario_id:
        return None
    for s in get_scenarios(company_config):
        if s.get("id") == scenario_id:
            return s
    return None


def get_card_extraction(company_config: dict) -> dict | None:
    """Generic structured-card config block ({label, prompt, json_schema}) or None."""
    return company_config.get("card_extraction")


def get_default_scenario_id(company_config: dict) -> str | None:
    """Tenant's default evaluation scenario id (used when the session has none)."""
    return company_config.get("default_scenario_id")
