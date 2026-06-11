import json
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.auth_user import require_viewer
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


@router.get("/{session_id}/transcript", dependencies=[Depends(require_viewer)])
async def get_transcript(session_id: uuid.UUID):
    return _read_result(session_id, "transcript.json")


@router.get("/{session_id}/analysis", dependencies=[Depends(require_viewer)])
async def get_analysis(session_id: uuid.UUID):
    return _read_result(session_id, "quality.json")
