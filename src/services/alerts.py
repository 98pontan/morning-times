"""Create alerts (stock moves, report days, important articles) and email the unsent ones."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.ai import ArticleInput, Summarizer, SummarizerError
from src.db.database import Database
from src.db.models import Alert, Article, Section, utcnow
from src.notifier import Notifier
from src.sections import get_plugin
from src.services.render import Renderer
from src.settings import AISettings

log = logging.getLogger(__name__)

# Only rate recent articles, so the first ingest doesn't flood the inbox with old news.
RATE_WINDOW = timedelta(days=2)
RATE_BATCH = 40


@dataclass
class AlertResult:
    created: int = 0
    rated: int = 0
    sent: int = 0


class AlertService:
    def __init__(self, db: Database, summarizer: Summarizer, notifier: Notifier,
                 settings: AISettings, renderer: Renderer):
        self.db = db
        self.summarizer = summarizer
        self.notifier = notifier
        self.settings = settings
        self.renderer = renderer

    def run(self) -> AlertResult:
        result = AlertResult()
        result.created += self._market_alerts()
        rated, created = self._article_alerts()
        result.rated, result.created = rated, result.created + created
        result.sent = self.send_pending()
        log.info("Alerts: %d created, %d articles rated, %d sent", result.created, result.rated, result.sent)
        return result

    def _market_alerts(self) -> int:
        created = 0
        with self.db.session() as session:
            sections = session.scalars(
                select(Section).where(Section.enabled).options(selectinload(Section.watch_items))
            ).all()
            for section in sections:
                for candidate in get_plugin(section.kind).check_alerts(section, section.watch_items):
                    created += self._add(session, Alert(
                        dedup_key=candidate.dedup_key, kind=candidate.kind, subject=candidate.subject,
                        body=candidate.body, watch_item_id=candidate.watch_item_id,
                    ))
        return created

    def _article_alerts(self) -> tuple[int, int]:
        cutoff = utcnow() - RATE_WINDOW
        with self.db.session() as session:
            articles = session.scalars(
                select(Article)
                .where(Article.rated_at.is_(None), Article.watch_items.any(), Article.published_at >= cutoff)
                .options(selectinload(Article.watch_items), selectinload(Article.source))
                .order_by(Article.published_at.desc())
                .limit(RATE_BATCH)
            ).all()
            if not articles:
                return 0, 0
            inputs = [
                ArticleInput(
                    id=a.id, title=a.title, summary=a.summary[:600],
                    source=a.source.name if a.source else "", published=a.published_at.isoformat(),
                    watched=[get_plugin(w.section.kind).describe_item(w) for w in a.watch_items],
                )
                for a in articles
            ]
            try:
                ratings = {r.article_id: r for r in self.summarizer.rate(inputs)}
            except SummarizerError:
                log.exception("Rating articles failed; will retry next run")
                return 0, 0

            created = 0
            now = utcnow()
            for article in articles:
                rating = ratings.get(article.id)
                if rating is None:
                    continue  # not rated this time; retried next run
                article.importance, article.importance_reason, article.rated_at = (
                    rating.importance, rating.reason, now
                )
                if rating.importance >= self.settings.alert_threshold:
                    names = ", ".join(w.name for w in article.watch_items)
                    created += self._add(session, Alert(
                        dedup_key=f"article:{article.url}"[:256],
                        kind="article",
                        subject=f"{names}: {article.title}"[:512],
                        body=f"{rating.reason}\nImportance {rating.importance}/10\n{article.url}",
                        watch_item_id=article.watch_items[0].id,
                        article_id=article.id,
                    ))
            return len(ratings), created

    @staticmethod
    def _add(session, alert: Alert) -> int:
        exists = session.scalar(select(Alert.id).where(Alert.dedup_key == alert.dedup_key))
        if exists:
            return 0
        session.add(alert)
        session.flush()
        return 1

    def send_pending(self) -> int:
        with self.db.session() as session:
            pending = session.scalars(
                select(Alert).where(Alert.sent_at.is_(None)).order_by(Alert.created_at)
            ).all()
            if not pending:
                return 0
            subject = pending[0].subject if len(pending) == 1 else f"{len(pending)} news alerts"
            text = "\n\n".join(f"{a.subject}\n{a.body}" for a in pending)
            try:
                self.notifier.send(subject, text, self.renderer.alerts_html(subject, pending))
            except Exception:
                log.exception("Sending alerts failed; will retry next run")
                return 0
            now = utcnow()
            for alert in pending:
                alert.sent_at = now
            return len(pending)
