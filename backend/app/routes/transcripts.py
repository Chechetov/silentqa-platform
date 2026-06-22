import json
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.auth_user import require_session_access
from app.config import settings
from tenancy.context import require_tenant_slug
from tenancy.paths import tenant_results_dir

router = APIRouter(prefix="/api/sessions", tags=["transcripts"])


def _read_result(session_id: uuid.UUID, filename: str) -> dict | list:
    result_path = tenant_results_dir(settings.RESULTS_STORAGE_PATH, require_tenant_slug(), session_id) / filename
    if not result_path.exists():
        raise HTTPException(status_code=404, detail=f"Result '{filename}' not found for this session")
    with open(result_path, encoding="utf-8") as f:
        return json.load(f)


@router.get("/{session_id}/transcript", dependencies=[Depends(require_session_access)])
async def get_transcript(session_id: uuid.UUID):
    return _read_result(session_id, "transcript.json")


@router.get("/{session_id}/analysis", dependencies=[Depends(require_session_access)])
async def get_analysis(session_id: uuid.UUID):
    return _read_result(session_id, "quality.json")


@router.get("/{session_id}/transcript-variants", dependencies=[Depends(require_session_access)])
async def get_transcript_variants(session_id: uuid.UUID):
    """ASR-сравнение: индекс вариантов + сами транскрипты по движкам.

    Возвращает {"engines": {engine: {status, segments, ...}}, "variants": {engine: [segments]}}.
    Если сравнение ещё не запускали — пустые объекты (200, не 404), чтобы UI показал
    «вариантов пока нет».
    """
    rd = tenant_results_dir(settings.RESULTS_STORAGE_PATH, require_tenant_slug(), session_id)
    index_path = rd / "transcript_variants.json"
    if not index_path.exists():
        return {"engines": {}, "variants": {}}
    with open(index_path, encoding="utf-8") as f:
        engines = json.load(f)
    variants: dict = {}
    for engine in engines:
        vpath = rd / f"transcript_{engine}.json"
        if vpath.exists():
            with open(vpath, encoding="utf-8") as f:
                variants[engine] = json.load(f)
    return {"engines": engines, "variants": variants}
