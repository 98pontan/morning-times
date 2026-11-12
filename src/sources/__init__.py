"""Article fetchers. Add a new source type by subclassing Fetcher and registering it."""

from __future__ import annotations

import calendar
import html
import re
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser

USER_AGENT = "morning-times/0.1 (+https://forgejo.pontus-homelab.space)"
_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    title: str
    summary: str
    published_at: datetime


class Fetcher(ABC):
    kind: str

    @abstractmethod
    def fetch(self, url: str) -> list[FetchedArticle]:
        """Return the articles currently available at the source."""


class RssFetcher(Fetcher):
    kind = "rss"

    def __init__(self, timeout_seconds: float = 15.0):
        self.timeout_seconds = timeout_seconds

    def fetch(self, url: str) -> list[FetchedArticle]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return self.parse(response.read())

    @staticmethod
    def parse(content: bytes | str) -> list[FetchedArticle]:
        feed = feedparser.parse(content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"Could not parse feed: {feed.bozo_exception}")
        articles = []
        for entry in feed.entries:
            link = entry.get("link")
            title = clean_text(entry.get("title", ""))
            if not link or not title:
                continue
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            published = (
                datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
                if parsed else datetime.now(timezone.utc)
            )
            summary = clean_text(entry.get("summary", ""))[:2000]
            articles.append(FetchedArticle(link, title[:512], summary, published))
        return articles


def clean_text(value: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", value)).split())


FETCHERS: dict[str, Fetcher] = {f.kind: f for f in [RssFetcher()]}


def get_fetcher(kind: str) -> Fetcher:
    try:
        return FETCHERS[kind]
    except KeyError:
        raise ValueError(f"Unknown source kind '{kind}'. Available: {sorted(FETCHERS)}") from None
