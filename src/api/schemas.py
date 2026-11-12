"""Request/response models for the REST API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- sections ----

class SectionIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    name: str = Field(min_length=1, max_length=128)
    kind: str
    enabled: bool = True
    position: int = 0
    settings: dict = Field(default_factory=dict)


class SectionPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    position: int | None = None
    settings: dict | None = None


class SectionOut(ORM):
    id: int
    slug: str
    name: str
    kind: str
    enabled: bool
    position: int
    settings: dict


class SectionKindOut(BaseModel):
    kind: str
    description: str
    item_types: list[str]


# ---- sources ----

class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(pattern=r"^https?://")
    kind: str = "rss"
    enabled: bool = True


class SourcePatch(BaseModel):
    name: str | None = None
    url: str | None = Field(default=None, pattern=r"^https?://")
    enabled: bool | None = None


class SourceOut(ORM):
    id: int
    section_id: int
    name: str
    url: str
    kind: str
    enabled: bool
    last_fetched_at: datetime | None
    last_error: str | None


# ---- watch items ----

class WatchItemIn(BaseModel):
    type: str
    name: str = Field(min_length=1, max_length=128)
    symbol: str | None = None
    keywords: list[str] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)
    enabled: bool = True


class WatchItemPatch(BaseModel):
    name: str | None = None
    symbol: str | None = None
    keywords: list[str] | None = None
    settings: dict | None = None
    enabled: bool | None = None


class WatchItemOut(ORM):
    id: int
    section_id: int
    type: str
    name: str
    symbol: str | None
    keywords: list[str]
    settings: dict
    enabled: bool


# ---- articles, briefs, alerts ----

class ArticleOut(ORM):
    id: int
    section_id: int
    url: str
    title: str
    summary: str
    source: str | None = None
    published_at: datetime
    importance: int | None
    importance_reason: str | None
    watched: list[str] = Field(default_factory=list)


class BriefOut(ORM):
    date: date
    generator: str
    created_at: datetime
    sent_at: datetime | None
    data: dict


class AlertOut(ORM):
    id: int
    kind: str
    subject: str
    body: str
    created_at: datetime
    sent_at: datetime | None
