"""Try several AI providers in order: the first that answers wins.

Free tiers have outages, "high demand" errors and daily quotas, so a second free provider makes the
AI text far more reliable than retrying one of them. If all fail, the caller's rules fallback applies.
"""

from __future__ import annotations

import logging
import threading
from datetime import date

from src.ai.base import ArticleInput, ArticleRating, BriefContent, OutlookText, SectionInput, Summarizer, SummarizerError

log = logging.getLogger(__name__)


class ChainSummarizer(Summarizer):
    def __init__(self, summarizers: list[Summarizer]):
        self.summarizers = summarizers
        self._used = threading.local()

    @property
    def name(self) -> str:
        """The provider that answered the last call in this thread, else the whole chain."""
        return getattr(self._used, "name", None) or "+".join(s.name for s in self.summarizers)

    def rate(self, articles: list[ArticleInput]) -> list[ArticleRating]:
        return self._first("rate", articles)

    def write_brief(self, day: date, sections: list[SectionInput]) -> BriefContent:
        return self._first("write_brief", day, sections)

    def energy_outlook(self, data: dict) -> OutlookText:
        return self._first("energy_outlook", data)

    def _first(self, method: str, *args):
        self._used.name = None
        errors = []
        for s in self.summarizers:
            try:
                result = getattr(s, method)(*args)
            except NotImplementedError:
                continue
            except Exception as exc:
                log.warning("%s.%s failed (%s); trying the next provider", s.name, method, exc)
                errors.append(f"{s.name}: {exc}")
                continue
            self._used.name = s.name
            return result
        raise SummarizerError("; ".join(errors) or f"no provider implements {method}")
