"""Google Gemini summarizer (google-genai SDK, Interactions API).

The free tier is fine for personal use from Sweden. Google's terms require paid services
when the app is made available to other users in the EEA, UK or Switzerland.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date

from pydantic import BaseModel, ValidationError

from src.ai.base import (
    BRIEF_SYSTEM, ENERGY_SYSTEM, RATING_SYSTEM, ArticleInput, ArticleRating, BriefContent, OutlookText, RatingResult, SectionInput,
    Summarizer, SummarizerError, clean_brief, clean_ratings,
)
from src.settings import AISettings

log = logging.getLogger(__name__)

# Gemini thinking levels are minimal/low/medium/high; map our effort names onto them.
_THINKING = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class GeminiSummarizer(Summarizer):
    name = "gemini"

    def __init__(self, settings: AISettings, client=None):
        if client is None:
            from google import genai

            client = genai.Client()  # reads GEMINI_API_KEY (or GOOGLE_API_KEY)
        self.settings = settings
        self.client = client

    def rate(self, articles: list[ArticleInput]) -> list[ArticleRating]:
        if not articles:
            return []
        payload = json.dumps([asdict(a) for a in articles], ensure_ascii=False, indent=1)
        result = self._generate(RATING_SYSTEM, f"<articles>\n{payload}\n</articles>",
                                RatingResult, self.settings.rating_effort)
        return clean_ratings(result.ratings, articles)

    def write_brief(self, day: date, sections: list[SectionInput]) -> BriefContent:
        payload = json.dumps({"date": day.isoformat(), "sections": [asdict(s) for s in sections]},
                             ensure_ascii=False, indent=1)
        brief = self._generate(BRIEF_SYSTEM.format(language=self.settings.language),
                               f"<news>\n{payload}\n</news>", BriefContent, self.settings.brief_effort)
        return clean_brief(brief, sections)

    def energy_outlook(self, data: dict) -> OutlookText:
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        return self._generate(ENERGY_SYSTEM.format(language=self.settings.language),
                              f"<data>\n{payload}\n</data>", OutlookText, "low")

    def _generate(self, system: str, user: str, output: type[BaseModel], effort: str):
        try:
            interaction = self.client.interactions.create(
                model=self.settings.gemini_model,
                system_instruction=system,
                input=user,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": output.model_json_schema()},
                generation_config={"thinking_level": _THINKING.get(effort, "medium")},
                store=False,  # don't keep the conversation on Google's side
            )
        except Exception as exc:  # quota (429), network, auth: let callers fall back
            raise SummarizerError(f"Gemini request failed: {exc}") from exc

        usage = getattr(interaction, "usage", None)
        log.info("Gemini %s: status=%s in=%s out=%s", output.__name__, interaction.status,
                 getattr(usage, "total_input_tokens", None), getattr(usage, "total_output_tokens", None))
        if interaction.status != "completed":
            raise SummarizerError(f"Gemini interaction ended with status '{interaction.status}'")
        if not interaction.output_text:
            raise SummarizerError("Gemini returned no output")
        try:
            return output.model_validate_json(interaction.output_text)
        except ValidationError as exc:
            raise SummarizerError(f"Gemini output did not match the schema: {exc}") from exc
