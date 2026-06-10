from datetime import datetime
import uuid

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    kind: str = Field(pattern="^(extraction|evaluation)$")
    prompt: str = Field(min_length=1)
    json_schema: dict


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    prompt: str | None = Field(default=None, min_length=1)
    json_schema: dict | None = None


class TemplateListItem(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    kind: str
    updated_at: datetime


class TemplateDetail(TemplateListItem):
    prompt: str
    json_schema: dict
    created_at: datetime


class ComplexListItem(BaseModel):
    id: uuid.UUID
    name: str
    developer: str | None
    class_: str | None = Field(alias="class")
    district: str | None
    sources_count: int
    updated_at: datetime

    model_config = {"populate_by_name": True}


class ComplexDetail(ComplexListItem):
    aggregated_data: dict
    sources: list[dict]


class ComplexRename(BaseModel):
    name: str = Field(min_length=1, max_length=300)


class ComplexMerge(BaseModel):
    target_complex_id: uuid.UUID


class ExtractionRelink(BaseModel):
    complex_id: uuid.UUID | None
