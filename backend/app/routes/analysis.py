import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth_user import require_admin, require_session_access
from app.config import settings
from tenancy.context import require_tenant_slug
from tenancy.paths import tenant_audio_sessions_dir, tenant_results_dir

router = APIRouter(prefix="/api/sessions", tags=["analysis"])


class ReassignSpeakerRequest(BaseModel):
    old_speaker: str
    new_speaker: str


def _read_result_file(session_id: uuid.UUID, filename: str) -> dict | list:
    result_path = tenant_results_dir(settings.RESULTS_STORAGE_PATH, require_tenant_slug(), session_id) / filename
    if not result_path.exists():
        raise HTTPException(status_code=404, detail=f"Result '{filename}' not found for this session")
    with open(result_path, encoding="utf-8") as f:
        return json.load(f)


@router.get("/{session_id}/audio", dependencies=[Depends(require_session_access)])
async def get_audio(session_id: uuid.UUID):
    base = tenant_audio_sessions_dir(settings.AUDIO_STORAGE_PATH, require_tenant_slug(), session_id)
    webm = base / "combined.webm"
    wav = base / "full.wav"
    if webm.exists():
        return FileResponse(webm, media_type="audio/webm", filename="combined.webm")
    if wav.exists():
        return FileResponse(wav, media_type="audio/wav", filename="full.wav")
    raise HTTPException(status_code=404, detail="Audio not found for this session")


@router.get("/{session_id}/sentiment", dependencies=[Depends(require_session_access)])
async def get_sentiment(session_id: uuid.UUID):
    return _read_result_file(session_id, "sentiment.json")


@router.get("/{session_id}/quality", dependencies=[Depends(require_session_access)])
async def get_quality(session_id: uuid.UUID):
    return _read_result_file(session_id, "quality.json")


@router.get("/{session_id}/card", dependencies=[Depends(require_session_access)])
async def get_card(session_id: uuid.UUID):
    """Structured «Карта приёма» (card.json). 404 if this tenant produces none."""
    return _read_result_file(session_id, "card.json")


@router.get("/{session_id}/full", dependencies=[Depends(require_session_access)])
async def get_full_result(session_id: uuid.UUID):
    transcript = _read_result_file(session_id, "transcript.json")
    sentiment = _read_result_file(session_id, "sentiment.json")
    quality = _read_result_file(session_id, "quality.json")
    return {
        "transcript": transcript,
        "sentiment": sentiment,
        "quality": quality,
    }


@router.patch("/{session_id}/reassign-speaker", dependencies=[Depends(require_admin)])
async def reassign_speaker(session_id: uuid.UUID, body: ReassignSpeakerRequest):
    """Reassign all segments from old_speaker to new_speaker in saved transcript."""
    result_dir = tenant_results_dir(settings.RESULTS_STORAGE_PATH, require_tenant_slug(), session_id)
    transcript_path = result_dir / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=404, detail="Transcript not found")

    with open(transcript_path, encoding="utf-8") as f:
        transcript = json.load(f)

    changed = 0
    for seg in transcript:
        if seg.get("speaker") == body.old_speaker:
            seg["speaker"] = body.new_speaker
            changed += 1

    if changed == 0:
        raise HTTPException(status_code=400, detail=f"Speaker '{body.old_speaker}' not found in transcript")

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False, indent=2)

    return {"changed_segments": changed, "old_speaker": body.old_speaker, "new_speaker": body.new_speaker}
