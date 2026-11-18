"""Summarizers write the morning brief and rate how important articles are.

Providers (ai.provider in config.yaml):
  claude      - Claude API, needs ANTHROPIC_API_KEY
  gemini      - Google Gemini API, needs GEMINI_API_KEY (free tier available)
  mistral     - Mistral, needs MISTRAL_API_KEY (paid, pay-as-you-go)
  openrouter  - OpenRouter, needs OPENROUTER_API_KEY (free models available)
  ollama      - a local Ollama, needs ai.ollama_url
  rules       - no AI: headline list and keyword ratings
  auto        - claude if ANTHROPIC_API_KEY is set, else the first of ai.fallbacks that is available

After the main provider, every available provider in ai.fallbacks is tried in order (see chain.py).
Add a provider by subclassing Summarizer (see claude.py / gemini.py) and adding it to build_summarizer;
an OpenAI-compatible service only needs an entry in openai_compat.PROVIDERS.
"""

from __future__ import annotations

import logging
import os

from src.ai.base import (
    ArticleInput, ArticleRating, BriefContent, BriefHighlight, BriefSection, RatingResult,
    SectionInput, Summarizer, SummarizerError,
)
from src.ai import openai_compat
from src.ai.chain import ChainSummarizer
from src.ai.claude import ClaudeSummarizer
from src.ai.gemini import GeminiSummarizer
from src.ai.openai_compat import OpenAICompatibleSummarizer
from src.ai.rules import RuleSummarizer
from src.settings import AISettings

log = logging.getLogger(__name__)

API_KEYS = {
    "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _has_key(provider: str, settings: AISettings | None = None) -> bool:
    if provider in openai_compat.PROVIDERS:
        return openai_compat.available(provider, settings or AISettings())
    return provider in API_KEYS and any(os.environ.get(name) for name in API_KEYS[provider])


def _make(provider: str, settings: AISettings) -> Summarizer:
    if provider == "claude":
        return ClaudeSummarizer(settings)
    if provider == "gemini":
        return GeminiSummarizer(settings)
    return OpenAICompatibleSummarizer(provider, settings)


def build_summarizer(settings: AISettings) -> Summarizer:
    provider = settings.provider
    if provider == "rules":
        return RuleSummarizer()
    order = (["claude"] if provider == "auto" else [provider]) + list(settings.fallbacks)
    names = [p for p in dict.fromkeys(order) if _has_key(p, settings)]
    if provider not in ("auto", *names):
        log.warning("ai.provider is %s but it is not configured (API key or URL missing)", provider)
    if not names:
        log.warning("No AI provider is configured; using the rules-based summarizer")
        return RuleSummarizer()
    summarizers = [_make(p, settings) for p in names]
    return summarizers[0] if len(summarizers) == 1 else ChainSummarizer(summarizers)


__all__ = [
    "ArticleInput", "ArticleRating", "BriefContent", "BriefHighlight", "BriefSection", "RatingResult",
    "SectionInput", "Summarizer", "SummarizerError", "ChainSummarizer", "ClaudeSummarizer", "GeminiSummarizer",
    "OpenAICompatibleSummarizer",
    "RuleSummarizer", "build_summarizer",
]
