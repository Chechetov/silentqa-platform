import asyncio
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Chunk, Session, SessionStatus
from app.schemas import ChunkResponse
from tenancy.context import require_tenant_slug
from tenancy.paths import tenant_audio_sessions_dir

# Hard cap so a malicious or buggy client cannot DoS missing-chunks (which
# materializes a range up to received[-1]) or blow the filename pad. 99999
# chunks at 10s each = ~277 hours, far beyond any realistic recording.
MAX_CHUNK_NUMBER = 99_999

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["chunks"])


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run ffmpeg and raise RuntimeError with stderr tail on non-zero exit."""
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"ffmpeg exit {result.returncode}: {stderr_tail}")


@router.post("/{session_id}/chunks", response_model=ChunkResponse, status_code=201)
async def upload_chunk(
    session_id: uuid.UUID,
    file: UploadFile,
    chunk_number: int | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Upload a single chunk of a recording session.

    `chunk_number` is the client's authoritative sequence number, starting at 1.
    Letting the client own numbering makes retries idempotent (replay of the
    same number overwrites the same file) and lets the client buffer chunks
    locally and dispatch them out of order if the network was down. If the
    client omits it, we fall back to the legacy `max+1` behavior so older
    builds keep working.
    """
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status not in (SessionStatus.created, SessionStatus.uploading):
        raise HTTPException(status_code=400, detail=f"Cannot upload to session in status '{session.status}'")

    # Update status to uploading
    if session.status == SessionStatus.created:
        session.status = SessionStatus.uploading

    if chunk_number is not None:
        if chunk_number < 1 or chunk_number > MAX_CHUNK_NUMBER:
            raise HTTPException(
                status_code=400,
                detail=f"chunk_number must be in [1, {MAX_CHUNK_NUMBER}]",
            )
    else:
        max_num = await db.scalar(
            select(func.coalesce(func.max(Chunk.chunk_number), 0)).where(Chunk.session_id == session_id)
        )
        chunk_number = max_num + 1
        if chunk_number > MAX_CHUNK_NUMBER:
            raise HTTPException(status_code=400, detail="chunk count exceeded")

    session_dir = tenant_audio_sessions_dir(settings.AUDIO_STORAGE_PATH, require_tenant_slug(), session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    file_path = session_dir / f"chunk_{chunk_number:06d}.webm"

    # Atomic write: write to a temp file, then rename. Two parallel uploads of
    # the same chunk_number won't tear each other's bytes mid-flight; the last
    # rename wins, matching the last DB upsert's intent.
    tmp_path = file_path.with_suffix(f".webm.tmp.{uuid.uuid4().hex[:8]}")
    content = await file.read()
    try:
        async with aiofiles.open(tmp_path, "wb") as f:
            await f.write(content)

        # Capture old size BEFORE rename so we can correct file_size_bytes on
        # overwrite (e.g. retry with a re-encoded payload of slightly different
        # length).
        try:
            old_size = file_path.stat().st_size
        except FileNotFoundError:
            old_size = None

        os.replace(tmp_path, file_path)
    except Exception:
        # Don't leave .tmp.<rand> orphans on disk if write/rename fails.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # Atomic upsert via Postgres ON CONFLICT — relies on the
    # UNIQUE(session_id, chunk_number) constraint added in migration 006.
    # If this fails after the file rename, the file is an orphan that the next
    # successful upsert (same chunk_number) will reclaim.
    try:
        stmt = (
            pg_insert(Chunk)
            .values(
                session_id=session_id,
                chunk_number=chunk_number,
                file_path=str(file_path),
            )
            .on_conflict_do_update(
                constraint="uq_chunks_session_number",
                set_={"file_path": str(file_path)},
            )
            .returning(Chunk.id, Chunk.uploaded_at)
        )
        row = (await db.execute(stmt)).first()
    except Exception:
        logger.warning(
            "chunk upsert failed for session=%s chunk=%s — file %s now orphaned",
            session_id, chunk_number, file_path,
        )
        raise

    # Atomic running total: avoid the lost-update where two parallel uploads
    # for DIFFERENT chunk_numbers each read stale file_size_bytes, add their
    # delta, and overwrite each other. Doing it as one SQL statement makes
    # Postgres serialize the read-modify-write inside the row lock.
    delta = len(content) if old_size is None else (len(content) - old_size)
    if delta != 0:
        await db.execute(
            text(
                "UPDATE sessions SET file_size_bytes = "
                "GREATEST(0, COALESCE(file_size_bytes, 0) + :delta) "
                "WHERE id = :sid"
            ),
            {"delta": delta, "sid": session_id},
        )

    await db.commit()

    return ChunkResponse(
        id=row.id,
        session_id=session_id,
        chunk_number=chunk_number,
        uploaded_at=row.uploaded_at,
    )


@router.get("/{session_id}/missing-chunks")
async def missing_chunks(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """List chunk numbers that should exist but don't.

    A "missing" chunk is one whose number is below the highest received but
    has no row. The client uses this to know what to re-upload from its local
    buffer before calling /finish. Empty list ⇒ contiguous sequence.
    """
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    rows = await db.execute(
        select(Chunk.chunk_number).where(Chunk.session_id == session_id).order_by(Chunk.chunk_number)
    )
    received = sorted({r[0] for r in rows.all()})
    if not received:
        return {"received": [], "missing": [], "max": 0}

    full = set(range(1, received[-1] + 1))
    missing = sorted(full - set(received))
    return {"received": received, "missing": missing, "max": received[-1]}


@router.post("/{session_id}/upload-audio")
async def upload_audio_file(
    session_id: uuid.UUID,
    file: UploadFile,
    template_id: uuid.UUID | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Upload a complete audio file (mp3, wav, m4a, webm, ogg, etc.).
    Converts to full.wav (for pipeline) and combined.webm (for player).
    """
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status not in (SessionStatus.created, SessionStatus.uploading):
        raise HTTPException(status_code=400, detail=f"Cannot upload to session in status '{session.status}'")

    # Determine extension from filename
    original_name = file.filename or "audio.bin"
    ext = Path(original_name).suffix.lower() or ".webm"

    # Save uploaded file
    session_dir = tenant_audio_sessions_dir(settings.AUDIO_STORAGE_PATH, require_tenant_slug(), session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    original_path = session_dir / f"original{ext}"

    async with aiofiles.open(original_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    file_size = len(content)
    session.file_size_bytes = file_size
    session.status = SessionStatus.uploading
    if template_id is not None:
        meta = dict(session.metadata_ or {})
        meta["template_id"] = str(template_id)
        session.metadata_ = meta

    # Create a chunk record for tracking
    chunk = Chunk(
        session_id=session_id,
        chunk_number=1,
        file_path=str(original_path),
    )
    db.add(chunk)
    await db.commit()

    # Convert to full.wav (16kHz mono for pipeline) and combined.webm (for player) in background
    wav_path = session_dir / "full.wav"
    webm_path = session_dir / "combined.webm"

    loop = asyncio.get_event_loop()

    async def convert():
        # Convert to WAV for pipeline
        await loop.run_in_executor(None, _run_ffmpeg, [
            "ffmpeg", "-y", "-i", str(original_path),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path),
        ])
        # Convert to WebM Opus for player (if original is not already webm)
        if ext != ".webm":
            await loop.run_in_executor(None, _run_ffmpeg, [
                "ffmpeg", "-y", "-i", str(original_path),
                "-c:a", "libopus", "-b:a", "64k", str(webm_path),
            ])
        else:
            # Original is webm, just copy
            shutil.copyfile(original_path, webm_path)

    try:
        await convert()
    except RuntimeError as e:
        logger.error("Audio conversion failed for session %s: %s", session_id, e)
        session.status = SessionStatus.failed
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Audio conversion failed: {e}")

    return {"status": "ok", "file_size": file_size, "filename": original_name}
