"""Free fallback without AI: keyword ratings and a headline-list brief."""

from __future__ import annotations

import re
from datetime import date

from src.ai.base import ArticleInput, ArticleRating, BriefContent, BriefHighlight, BriefSection, SectionInput, Summarizer

_STRONG_WORDS = re.compile(
    r"\b(vinstvarning|kvartalsrapport|delårsrapport|bokslut|förvärv|uppköp|varslar|avgår|konkurs|"
    r"profit warning|earnings|acquisition|takeover|layoffs|resigns|bankrupt)\b",
    re.IGNORECASE,
)


class RuleSummarizer(Summarizer):
    """No AI: keyword-based ratings and a headline list as the brief.

    Ratings stay at 6 or below on purpose: keywords cannot judge importance, so without
    Claude only market alerts (price moves, report days) trigger emails, not articles.
    """

    name = "rules"

    def rate(self, articles: list[ArticleInput]) -> list[ArticleRating]:
        ratings = []
        for a in articles:
            strong = _STRONG_WORDS.search(f"{a.title} {a.summary}")
            ratings.append(ArticleRating(
                article_id=a.id,
                importance=6 if strong else 4,
                reason=f"Mentions {', '.join(a.watched)}" + (f" and '{strong.group(0)}'" if strong else ""),
            ))
        return ratings

    def write_brief(self, day: date, sections: list[SectionInput]) -> BriefContent:
        out = []
        for s in sections:
            if not s.articles:
                continue
            # Articles about watched items first, newest first within each group (published is ISO 8601).
            ranked = sorted(s.articles, key=lambda a: (bool(a.watched), a.published), reverse=True)
            out.append(BriefSection(
                slug=s.slug, title=s.name, summary="",
                highlights=[BriefHighlight(article_id=a.id, text=a.title) for a in ranked[:6]],
            ))
        return BriefContent(headline=f"Morgonbrief {day.isoformat()}", intro="", sections=out)
