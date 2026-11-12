"""Database models. Everything the frontend can configure lives here."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


article_watch_items = Table(
    "article_watch_items",
    Base.metadata,
    Column("article_id", ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("watch_item_id", ForeignKey("watch_items.id", ondelete="CASCADE"), primary_key=True),
)


class Section(Base):
    """A part of the brief/news site, e.g. Economy. `kind` selects the plugin in src/sections."""

    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Kind-specific options, e.g. {"move_threshold_pct": 5} for economy.
    settings: Mapped[dict] = mapped_column(JSON, default=dict)

    sources: Mapped[list[Source]] = relationship(back_populates="section", cascade="all, delete-orphan")
    watch_items: Mapped[list[WatchItem]] = relationship(back_populates="section", cascade="all, delete-orphan")


class Source(Base):
    """Where articles come from. `kind` selects the fetcher in src/sources (currently rss)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(32), default="rss")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    section: Mapped[Section] = relationship(back_populates="sources")


class WatchItem(Base):
    """Something to follow inside a section: a stock, a team, a region or a keyword."""

    __tablename__ = "watch_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    # Stock ticker (Yahoo Finance format, e.g. VOLV-B.ST) or league for teams.
    symbol: Mapped[str | None] = mapped_column(String(64))
    # Words that mark an article as being about this item (the name is always included).
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    section: Mapped[Section] = relationship(back_populates="watch_items")


class Article(Base):
    __tablename__ = "articles"
    # The same story can show up in several sections (e.g. SVT main feed and SVT Ekonomi).
    __table_args__ = (UniqueConstraint("section_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    url: Mapped[str] = mapped_column(String(1024), index=True)
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 1-10, set by the summarizer for articles about watched items.
    importance: Mapped[int | None] = mapped_column(Integer)
    importance_reason: Mapped[str | None] = mapped_column(Text)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    section: Mapped[Section] = relationship()
    source: Mapped[Source | None] = relationship()
    watch_items: Mapped[list[WatchItem]] = relationship(secondary=article_watch_items)


class Brief(Base):
    """The generated morning brief for one day."""

    __tablename__ = "briefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    data: Mapped[dict] = mapped_column(JSON)
    html: Mapped[str] = mapped_column(Text)
    generator: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Alert(Base):
    """An important event (big stock move, report day, important article). Emailed once."""

    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("dedup_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(32))
    watch_item_id: Mapped[int | None] = mapped_column(ForeignKey("watch_items.id", ondelete="SET NULL"))
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"))
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
