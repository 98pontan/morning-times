"""Fetch articles from all enabled sources and tag them with the watch items they mention."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.db.database import Database
from src.db.models import Article, Section, Source, utcnow
from src.sections import get_plugin
from src.sources import get_fetcher

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    sources: int = 0
    new_articles: int = 0
    errors: dict[str, str] = field(default_factory=dict)


class IngestService:
    def __init__(self, db: Database):
        self.db = db

    def run(self) -> IngestResult:
        with self.db.session() as session:
            source_ids = session.scalars(
                select(Source.id).join(Section)
                .where(Source.enabled, Section.enabled)
                .order_by(Section.position, Source.id)
            ).all()

        result = IngestResult(sources=len(source_ids))
        for source_id in source_ids:
            # One transaction per source, so a broken feed doesn't lose the others.
            with self.db.session() as session:
                source = session.get(Source, source_id)
                try:
                    result.new_articles += self._ingest_source(session, source)
                    source.last_error = None
                except Exception as exc:
                    log.warning("Fetching %s failed: %s", source.url, exc)
                    source.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                    result.errors[source.name] = source.last_error
                source.last_fetched_at = utcnow()
        log.info("Ingest: %d new articles from %d sources (%d errors)",
                 result.new_articles, result.sources, len(result.errors))
        return result

    def _ingest_source(self, session, source: Source) -> int:
        fetched = get_fetcher(source.kind).fetch(source.url)
        if not fetched:
            return 0
        section = session.scalar(
            select(Section).where(Section.id == source.section_id).options(selectinload(Section.watch_items))
        )
        plugin = get_plugin(section.kind)
        urls = {f.url for f in fetched}
        existing = set(session.scalars(
            select(Article.url).where(Article.section_id == section.id, Article.url.in_(urls))
        ))
        added = 0
        for item in fetched:
            if item.url in existing:
                continue
            existing.add(item.url)
            article = Article(
                section_id=section.id, source_id=source.id, url=item.url, title=item.title,
                summary=item.summary, published_at=item.published_at,
            )
            article.watch_items = plugin.match(article, section.watch_items)
            session.add(article)
            added += 1
        return added


REMATCH_WINDOW = timedelta(days=2)


def rematch_section(session, section: Section) -> int:
    """Re-tag a section's recent articles after its watch items changed. Returns articles re-tagged."""
    plugin = get_plugin(section.kind)
    session.refresh(section, ["watch_items"])
    articles = session.scalars(
        select(Article)
        .where(Article.section_id == section.id, Article.published_at >= utcnow() - REMATCH_WINDOW)
        .options(selectinload(Article.watch_items))
    ).all()
    changed = 0
    for article in articles:
        matched = plugin.match(article, section.watch_items)
        if {w.id for w in matched} != {w.id for w in article.watch_items}:
            article.watch_items = matched
            changed += 1
    return changed
