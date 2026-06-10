import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class SessionCreate(BaseModel):
    metadata: dict | None = None


class SessionResponse(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    file_size_bytes: int | None = None
    metadata: dict | None = None
    chunks_count: int = 0

    model_config = {"from_attributes": True}


class ChunkResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    chunk_number: int
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class SessionStatus(BaseModel):
    status: str


class SpeakerInfo(BaseModel):
    role: str | None = None
    name: str | None = None


class SpeakerMapUpdate(BaseModel):
    speaker_map: dict[str, SpeakerInfo]


# --- Broker authentication ---------------------------------------------------

class BrokerClaimStartRequest(BaseModel):
    email: EmailStr


class BrokerClaimStartResponse(BaseModel):
    name: str
    masked_email: str


class BrokerClaimCompleteRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class BrokerLoginRequest(BaseModel):
    email: EmailStr
    password: str


class BrokerInfo(BaseModel):
    id: uuid.UUID
    amocrm_user_id: int
    email: str
    name: str

    model_config = {"from_attributes": True}


class BrokerTokenResponse(BaseModel):
    token: str
    broker: BrokerInfo
