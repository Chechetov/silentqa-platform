import json
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth_platform import require_platform_admin

router = APIRouter(prefix="/api/companies", tags=["companies"],
                   dependencies=[Depends(require_platform_admin)])

COMPANIES_DIR = Path(os.getenv("COMPANIES_PATH", "/companies"))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CompanyCreate(BaseModel):
    id: str
    name: str
    asr: dict | None = None
    quality: dict | None = None
    extra: dict | None = Field(None, alias="extra")

    model_config = {"extra": "allow"}


class WordBoostUpdate(BaseModel):
    words: list[str] | None = None
    add: list[str] | None = None
    remove: list[str] | None = None


class ProtocolUpdate(BaseModel):
    protocol: str


class AsrUpdate(BaseModel):
    engine: Literal["whisper", "assemblyai"] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_config(company_id: str) -> dict:
    config_path = COMPANIES_DIR / f"{company_id}.json"
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="Company not found")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_config(company_id: str, config: dict) -> None:
    config_path = COMPANIES_DIR / f"{company_id}.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_companies():
    """List all available company configs."""
    companies = []
    if COMPANIES_DIR.exists():
        for f in sorted(COMPANIES_DIR.glob("*.json")):
            with open(f, "r", encoding="utf-8") as fh:
                config = json.load(fh)
            companies.append({
                "id": config.get("id", f.stem),
                "name": config.get("name", f.stem),
                "word_boost_count": len(config.get("asr", {}).get("word_boost", [])),
            })
    return companies


@router.get("/{company_id}")
async def get_company(company_id: str):
    """Get full company config."""
    return _read_config(company_id)


# ---------------------------------------------------------------------------
# POST / PUT / PATCH / DELETE endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def create_company(body: CompanyCreate):
    """Create a new company config."""
    config_path = COMPANIES_DIR / f"{body.id}.json"
    if config_path.exists():
        raise HTTPException(status_code=409, detail="Company already exists")
    config = body.model_dump(by_alias=True, exclude_none=True)
    _write_config(body.id, config)
    return config


@router.put("/{company_id}")
async def update_company(company_id: str, body: dict):
    """Replace full company config."""
    config_path = COMPANIES_DIR / f"{company_id}.json"
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="Company not found")
    body.setdefault("id", company_id)
    _write_config(company_id, body)
    return body


@router.patch("/{company_id}/word-boost")
async def update_word_boost(company_id: str, body: WordBoostUpdate):
    """Update word_boost list (full replace or partial add/remove)."""
    config = _read_config(company_id)
    asr = config.setdefault("asr", {})
    current: list[str] = asr.get("word_boost", [])

    if body.words is not None:
        # Full replacement
        current = body.words
    else:
        if body.add:
            current = current + [w for w in body.add if w not in current]
        if body.remove:
            remove_set = set(body.remove)
            current = [w for w in current if w not in remove_set]

    asr["word_boost"] = current
    _write_config(company_id, config)
    return {"word_boost": current}


@router.put("/{company_id}/protocol")
async def update_protocol(company_id: str, body: ProtocolUpdate):
    """Update quality evaluation protocol."""
    config = _read_config(company_id)
    quality = config.setdefault("quality", {})
    quality["protocol"] = body.protocol
    _write_config(company_id, config)
    return quality


@router.patch("/{company_id}/asr")
async def update_asr(company_id: str, body: AsrUpdate):
    """Update ASR settings."""
    config = _read_config(company_id)
    asr = config.setdefault("asr", {})
    asr["engine"] = body.engine
    _write_config(company_id, config)
    return asr


@router.delete("/{company_id}", status_code=204)
async def delete_company(company_id: str):
    """Delete a company config. The 'default' company cannot be deleted."""
    if company_id == "default":
        raise HTTPException(status_code=403, detail="Cannot delete default company")
    config_path = COMPANIES_DIR / f"{company_id}.json"
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="Company not found")
    config_path.unlink()
    return None
