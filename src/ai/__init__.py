"""Summarizers write the morning brief and rate how important articles are.

Providers (ai.provider in config.yaml):
  claude  - Claude API, needs ANTHROPIC_API_KEY
  gemini  - Google Gemini API, needs GEMINI_API_KEY (free tier available)
  rules   - no AI: headline list and keyword ratings
  auto    - claude if ANTHROPIC_API_KEY is set, else gemini if GEMINI_API_KEY is set, else rules

Add a provider by subclassing Summarizer (see claude.py / gemini.py) and adding it to build_summarizer.
"""

from __future__ import annotations

import logging
import os

from src.ai.base import (
    ArticleInput, ArticleRating, BriefContent, BriefHighlight, BriefSection, RatingResult,
    SectionInput, Summarizer, SummarizerError,
)
from src.ai.claude import ClaudeSummarizer
from src.ai.gemini import GeminiSummarizer
from src.ai.rules import RuleSummarizer
from src.settings import AISettings

log = logging.getLogger(__name__)

API_KEYS = {
    "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _has_key(provider: str) -> bool:
    return any(os.environ.get(name) for name in API_KEYS[provider])


def build_summarizer(settings: AISettings) -> Summarizer:
    provider = settings.provider
    if provider == "auto":
        provider = next((p for p in ("claude", "gemini") if _has_key(p)), "rules")
    if provider == "rules":
        return RuleSummarizer()
    if not _has_key(provider):
        log.warning("ai.provider is %s but %s is not set; using the rules-based summarizer",
                    provider, " or ".join(API_KEYS[provider]))
        return RuleSummarizer()
    return ClaudeSummarizer(settings) if provider == "claude" else GeminiSummarizer(settings)


__all__ = [
    "ArticleInput", "ArticleRating", "BriefContent", "BriefHighlight", "BriefSection", "RatingResult",
    "SectionInput", "Summarizer", "SummarizerError", "ClaudeSummarizer", "GeminiSummarizer",
    "RuleSummarizer", "build_summarizer",
]
