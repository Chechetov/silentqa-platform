import uuid

from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    description: str | None = None
    feeds_asr: bool = False
    feeds_llm: bool = False
    is_taxonomy: bool = False


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    feeds_asr: bool | None = None
    feeds_llm: bool | None = None
    is_taxonomy: bool | None = None


class EntryCreate(BaseModel):
    category_id: uuid.UUID
    term: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    metadata: dict = Field(default_factory=dict)


class EntryUpdate(BaseModel):
    term: str | None = Field(default=None, min_length=1, max_length=300)
    aliases: list[str] | None = None
    description: str | None = None
    metadata: dict | None = None


class ImportRow(BaseModel):
    term: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None


class ImportRequest(BaseModel):
    category_id: uuid.UUID
    rows: list[ImportRow] = Field(max_length=5000)
