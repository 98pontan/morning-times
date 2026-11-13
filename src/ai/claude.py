"""Claude API summarizer (Anthropic SDK)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date

from pydantic import BaseModel

from src.ai.base import (
    BRIEF_SYSTEM, ENERGY_SYSTEM, RATING_SYSTEM, ArticleInput, ArticleRating, BriefContent, OutlookText, RatingResult, SectionInput,
    Summarizer, SummarizerError, clean_brief, clean_ratings,
)
from src.settings import AISettings

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

class ClaudeSummarizer(Summarizer):
    name = "claude"

    def __init__(self, settings: AISettings, client=None):
        import anthropic

        self.settings = settings
        self.client = client or anthropic.Anthropic()

    def rate(self, articles: list[ArticleInput]) -> list[ArticleRating]:
        if not articles:
            return []
        payload = json.dumps([asdict(a) for a in articles], ensure_ascii=False, indent=1)
        result = self._parse(
            system=RATING_SYSTEM,
            user=f"<articles>\n{payload}\n</articles>",
            output_format=RatingResult,
            effort=self.settings.rating_effort,
        )
        return clean_ratings(result.ratings, articles)

    def write_brief(self, day: date, sections: list[SectionInput]) -> BriefContent:
        payload = json.dumps(
            {"date": day.isoformat(), "sections": [asdict(s) for s in sections]},
            ensure_ascii=False, indent=1,
        )
        brief = self._parse(
            system=BRIEF_SYSTEM.format(language=self.settings.language),
            user=f"<news>\n{payload}\n</news>",
            output_format=BriefContent,
            effort=self.settings.brief_effort,
        )
        return clean_brief(brief, sections)

    def energy_outlook(self, data: dict) -> OutlookText:
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        return self._parse(
            system=ENERGY_SYSTEM.format(language=self.settings.language),
            user=f"<data>\n{payload}\n</data>",
            output_format=OutlookText,
            effort="low",
        )

    def _parse(self, system: str, user: str, output_format: type[BaseModel], effort: str):
        response = self.client.beta.messages.parse(
            model=self.settings.claude_model,
            max_tokens=16000,
            # If a request is declined by a safety classifier, the API retries it on a fallback model.
            betas=[FALLBACK_BETA],
            fallbacks="default",
            # The system prompt is identical across calls; cached when long enough for the model's minimum.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": effort},
            output_format=output_format,
            messages=[{"role": "user", "content": user}],
        )
        usage = response.usage
        log.info(
            "Claude %s: in=%s cached=%s out=%s stop=%s request=%s",
            output_format.__name__, usage.input_tokens, usage.cache_read_input_tokens,
            usage.output_tokens, response.stop_reason, response._request_id,
        )
        if response.stop_reason == "refusal":
            raise SummarizerError(f"Claude declined the request ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise SummarizerError("Claude response was cut off (max_tokens)")
        if response.parsed_output is None:
            raise SummarizerError("Claude returned no structured output")
        return response.parsed_output
