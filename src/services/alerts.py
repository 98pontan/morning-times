"""Create alerts (stock moves, report days, important articles) and email the unsent ones.

Alerts belong to the owner of the watch that triggered them and go to that user's email. Default
watches (no owner) only alert when login is off (`default_alerts`); they then go to email.recipients.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.ai import ArticleInput, Summarizer, SummarizerError
from src.db.database import Database
from src.db.models import Alert, Article, Section, User, WatchItem, utcnow
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
                 settings: AISettings, renderer: Renderer, default_alerts: bool = True):
        self.db = db
        self.summarizer = summarizer
        self.notifier = notifier
        self.settings = settings
        self.renderer = renderer
        self.default_alerts = default_alerts

    def _alerting(self, item: WatchItem) -> bool:
        return item.owner_id is not None or self.default_alerts

    @staticmethod
    def _key(owner_id: int | None, key: str) -> str:
        return (key if owner_id is None else f"u{owner_id}:{key}")[:256]

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
                by_owner: dict[int | None, list[WatchItem]] = {}
                for item in section.watch_items:
                    if self._alerting(item):
                        by_owner.setdefault(item.owner_id, []).append(item)
                for owner_id, items in by_owner.items():
                    for candidate in get_plugin(section.kind).check_alerts(section, items):
                        created += self._add(session, Alert(
                            dedup_key=self._key(owner_id, candidate.dedup_key), kind=candidate.kind,
                            subject=candidate.subject, body=candidate.body,
                            watch_item_id=candidate.watch_item_id, user_id=owner_id,
                        ))
        return created

    def _article_alerts(self) -> tuple[int, int]:
        cutoff = utcnow() - RATE_WINDOW
        with self.db.session() as session:
            articles = session.scalars(
                select(Article)
                .where(Article.rated_at.is_(None), Article.published_at >= cutoff,
                       Article.watch_items.any() if self.default_alerts
                       else Article.watch_items.any(WatchItem.owner_id.is_not(None)))
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
                    watched=sorted({get_plugin(w.section.kind).describe_item(w) for w in a.watch_items}),
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
                if rating.importance < self.settings.alert_threshold:
                    continue
                # One alert per user whose watches matched, naming only that user's watches.
                by_owner: dict[int | None, list[WatchItem]] = {}
                for w in article.watch_items:
                    if self._alerting(w):
                        by_owner.setdefault(w.owner_id, []).append(w)
                for owner_id, items in by_owner.items():
                    names = ", ".join(dict.fromkeys(w.name for w in items))
                    created += self._add(session, Alert(
                        dedup_key=self._key(owner_id, f"article:{article.url}"),
                        kind="article",
                        subject=f"{names}: {article.title}"[:512],
                        body=f"{rating.reason}\nImportance {rating.importance}/10\n{article.url}",
                        watch_item_id=items[0].id,
                        article_id=article.id,
                        user_id=owner_id,
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
        """One email per recipient: each user gets their own alerts, default alerts go to email.recipients."""
        sent = 0
        with self.db.session() as session:
            pending = session.scalars(
                select(Alert).where(Alert.sent_at.is_(None)).order_by(Alert.user_id, Alert.created_at)
            ).all()
            for user_id, group in groupby(pending, key=lambda a: a.user_id):
                alerts = list(group)
                recipients = None
                if user_id is not None:
                    user = session.get(User, user_id)
                    if not user or not user.email:
                        log.warning("User %s has no email in Keycloak; dropping %d alert(s)",
                                    user.username if user else user_id, len(alerts))
                        for alert in alerts:
                            alert.sent_at = utcnow()
                        continue
                    recipients = [user.email]
                subject = alerts[0].subject if len(alerts) == 1 else f"{len(alerts)} news alerts"
                text = "\n\n".join(f"{a.subject}\n{a.body}" for a in alerts)
                try:
                    self.notifier.send(subject, text, self.renderer.alerts_html(subject, alerts), recipients=recipients)
                except Exception:
                    log.exception("Sending alerts failed; will retry next run")
                    continue
                now = utcnow()
                for alert in alerts:
                    alert.sent_at = now
                sent += len(alerts)
        return sent
