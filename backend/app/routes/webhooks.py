import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl

from app.config import settings

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

WEBHOOKS_FILE = Path(settings.RESULTS_STORAGE_PATH) / "webhooks.json"

ALLOWED_EVENTS = {"session.completed", "session.failed"}


class WebhookCreate(BaseModel):
    url: HttpUrl
    events: list[Literal["session.completed", "session.failed"]]


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    created_at: str


def _load_webhooks() -> list[dict]:
    if not WEBHOOKS_FILE.exists():
        return []
    with open(WEBHOOKS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_webhooks(webhooks: list[dict]) -> None:
    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEBHOOKS_FILE, "w", encoding="utf-8") as f:
        json.dump(webhooks, f, indent=2, ensure_ascii=False)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks():
    return _load_webhooks()


@router.post("", response_model=WebhookResponse, status_code=201)
async def create_webhook(body: WebhookCreate):
    webhooks = _load_webhooks()
    webhook = {
        "id": str(uuid.uuid4()),
        "url": str(body.url),
        "events": body.events,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    webhooks.append(webhook)
    _save_webhooks(webhooks)
    return webhook


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str):
    webhooks = _load_webhooks()
    filtered = [w for w in webhooks if w["id"] != webhook_id]
    if len(filtered) == len(webhooks):
        raise HTTPException(status_code=404, detail="Webhook not found")
    _save_webhooks(filtered)
