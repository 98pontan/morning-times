"""Section plugins decide what a section can watch and what counts as an alert."""

from __future__ import annotations

import re
from abc import ABC
from dataclasses import dataclass
from functools import lru_cache

from src.db.models import Article, Section, WatchItem


@dataclass
class AlertCandidate:
    dedup_key: str
    kind: str
    subject: str
    body: str
    watch_item_id: int | None = None


@lru_cache(maxsize=2048)
def _pattern(term: str) -> re.Pattern:
    # Whole word, case-insensitive, allowing a Swedish genitive: "Volvo" matches "Volvos" and "AIK" matches "AIK:s",
    # but "Ericsson" does not match inside "Ericssonfonden".
    return re.compile(rf"(?<!\w){re.escape(term)}(?::?s)?(?!\w)", re.IGNORECASE)


class SectionPlugin(ABC):
    kind: str = ""
    description: str = ""
    # Watch item types this section accepts, e.g. {"stock", "keyword"}.
    item_types: frozenset[str] = frozenset({"keyword"})

    def terms(self, item: WatchItem) -> list[str]:
        terms = [item.name, *(item.keywords or [])]
        return [t.strip() for t in terms if t and len(t.strip()) >= 2]

    def match(self, article: Article, items: list[WatchItem]) -> list[WatchItem]:
        """Watch items this article is about (keyword match on title + summary)."""
        text = f"{article.title}\n{article.summary}"
        return [
            item for item in items
            if item.enabled and any(_pattern(t).search(text) for t in self.terms(item))
        ]

    def check_alerts(self, section: Section, items: list[WatchItem]) -> list[AlertCandidate]:
        """Alerts that do not come from articles, e.g. stock price moves. None by default."""
        return []

    def describe_item(self, item: WatchItem) -> str:
        """Short description used in prompts, e.g. 'Volvo (VOLV-B.ST)'."""
        return f"{item.name} ({item.symbol})" if item.symbol else item.name
