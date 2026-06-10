import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.config import settings

router = APIRouter(prefix="/api/sessions", tags=["transcripts"])


def _read_result(session_id: uuid.UUID, filename: str) -> dict | list:
    result_path = Path(settings.RESULTS_STORAGE_PATH) / str(session_id) / filename
    if not result_path.exists():
        raise HTTPException(status_code=404, detail=f"Result '{filename}' not found for this session")
    with open(result_path, encoding="utf-8") as f:
        return json.load(f)


@router.get("/{session_id}/transcript")
async def get_transcript(session_id: uuid.UUID):
    return _read_result(session_id, "transcript.json")


@router.get("/{session_id}/analysis")
async def get_analysis(session_id: uuid.UUID):
    return _read_result(session_id, "quality.json")
