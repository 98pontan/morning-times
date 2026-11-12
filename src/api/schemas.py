"""Request/response models for the REST API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

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


# ---- regions (catalog of local editions for regional sections) ----

class RegionOut(BaseModel):
    slug: str
    name: str
    covers: str
    url: str
    # The section's source for this region, if it was ever picked.
    source_id: int | None = None
    enabled: bool = False


class RegionPatch(BaseModel):
    enabled: bool


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


# ---- stock screen ----

class StockRowOut(ORM):
    name: str
    symbol: str
    watch_item_id: int | None = None
    currency: str = ""
    last: float | None = None
    change: float | None = None
    change_pct: float | None = None
    # Development in % over 1w, 1m, 3m, ytd and 1y (null when there is no data that far back).
    returns: dict[str, float | None] = Field(default_factory=dict)
    high_52w: float | None = None
    low_52w: float | None = None
    volume: float | None = None
    turnover: float | None = None
    earnings_date: date | None = None
    as_of: datetime | None = None
    # Daily closes for the last 3 months, oldest first.
    spark: list[float] = Field(default_factory=list)
    error: str | None = None


class MarketOverviewOut(ORM):
    index: StockRowOut | None
    stocks: list[StockRowOut]
    updated_at: datetime


class ChartPointOut(ORM):
    t: datetime
    close: float


class ChartOut(BaseModel):
    symbol: str
    range: str
    points: list[ChartPointOut]


# ---- weather (weather sections) ----

class WarningAreaOut(BaseModel):
    id: int                      # SMHI area id
    name: str
    kind: str                    # county | sea
    watch_item_id: int | None = None
    enabled: bool = False
    # Levels that are emailed for this area (MESSAGE, YELLOW, ORANGE, RED).
    levels: list[str] = Field(default_factory=list)


class WarningAreaPatch(BaseModel):
    enabled: bool
    # Omit to keep the current levels (or the section default for a new area).
    levels: list[Literal["MESSAGE", "YELLOW", "ORANGE", "RED"]] | None = None


class WarningOut(BaseModel):
    id: int
    event: str
    event_code: str
    level: str
    level_name: str
    area_name: str
    areas: list[str]             # watched counties/sea districts it touches
    alerting: bool               # emailed for at least one of those areas
    start: datetime | None = None
    end: datetime | None = None
    published: datetime | None = None
    descriptions: dict[str, str] = Field(default_factory=dict)


class ForecastStepOut(ORM):
    time: datetime
    hours: float
    temperature: float | None
    symbol: int | None
    symbol_text: str
    precipitation: float | None  # mm during the step
    wind_speed: float | None
    wind_gust: float | None
    wind_direction: float | None


class DayForecastOut(ORM):
    date: date
    min_temp: float | None
    max_temp: float | None
    precipitation: float
    symbol: int | None
    symbol_text: str
    max_wind: float | None


class PlaceForecastOut(ORM):
    name: str
    lat: float
    lon: float
    watch_item_id: int | None = None
    now: ForecastStepOut | None = None
    hours: list[ForecastStepOut] = Field(default_factory=list)
    days: list[DayForecastOut] = Field(default_factory=list)
    error: str | None = None


class WeatherOverviewOut(BaseModel):
    places: list[PlaceForecastOut]
    warnings: list[WarningOut]
    updated_at: datetime
    error: str | None = None


# ---- articles, briefs, alerts ----

class ArticleOut(ORM):
    id: int
    section_id: int
    source_id: int | None = None
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
