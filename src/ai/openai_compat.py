"""Summarizer for any OpenAI-compatible chat API: Mistral, OpenRouter or a local Ollama.

Small free models are fine for this app's three jobs. JSON mode plus the schema in the prompt is
the lowest common denominator these services share; the answer is validated with pydantic.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import date

import httpx
from pydantic import BaseModel, ValidationError

from src.ai.base import (
    BRIEF_SYSTEM, ENERGY_SYSTEM, RATING_SYSTEM, ArticleInput, ArticleRating, BriefContent, OutlookText, RatingResult,
    SectionInput, Summarizer, SummarizerError, clean_brief, clean_ratings,
)
from src.settings import AISettings

log = logging.getLogger(__name__)

# provider -> (base URL, environment variable with the API key; None = no key needed)
PROVIDERS: dict[str, tuple[str, str | None]] = {
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("", None),  # base URL from ai.ollama_url, e.g. http://ollama.ollama:11434/v1
}


def available(provider: str, settings: AISettings) -> bool:
    url, key_env = PROVIDERS[provider]
    if provider == "ollama":
        return bool(settings.ollama_url)
    return bool(key_env and os.environ.get(key_env))


class OpenAICompatibleSummarizer(Summarizer):
    def __init__(self, provider: str, settings: AISettings, client: httpx.Client | None = None):
        url, key_env = PROVIDERS[provider]
        self.name = provider
        self.settings = settings
        self.model = settings.models.get(provider, "")
        self.base_url = (settings.ollama_url if provider == "ollama" else url).rstrip("/")
        key = os.environ.get(key_env, "") if key_env else ""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.client = client or httpx.Client(headers=headers, timeout=120)

    def rate(self, articles: list[ArticleInput]) -> list[ArticleRating]:
        if not articles:
            return []
        payload = json.dumps([asdict(a) for a in articles], ensure_ascii=False, indent=1)
        result = self._generate(RATING_SYSTEM, f"<articles>\n{payload}\n</articles>", RatingResult)
        return clean_ratings(result.ratings, articles)

    def write_brief(self, day: date, sections: list[SectionInput]) -> BriefContent:
        payload = json.dumps({"date": day.isoformat(), "sections": [asdict(s) for s in sections]},
                             ensure_ascii=False, indent=1)
        brief = self._generate(BRIEF_SYSTEM.format(language=self.settings.language),
                               f"<news>\n{payload}\n</news>", BriefContent)
        return clean_brief(brief, sections)

    def energy_outlook(self, data: dict) -> OutlookText:
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        return self._generate(ENERGY_SYSTEM.format(language=self.settings.language),
                              f"<data>\n{payload}\n</data>", OutlookText)

    def _generate(self, system: str, user: str, output: type[BaseModel]):
        schema = json.dumps(output.model_json_schema(), ensure_ascii=False)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system}\n\nAnswer with one JSON object matching this JSON schema:\n{schema}"},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
        }
        if self.name == "openrouter" and self.model != "openrouter/free":
            # OpenRouter tries these in order, so a retired free model does not break the fallback.
            body["models"] = [self.model, "openrouter/free"]
        try:
            resp = self.client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:  # network, quota (429), auth, odd responses: let callers fall back
            raise SummarizerError(f"{self.name} request failed: {exc}") from exc
        usage = data.get("usage") or {}
        log.info("%s %s: in=%s out=%s", self.name, output.__name__, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        try:
            return output.model_validate_json(_strip_fence(text or ""))
        except ValidationError as exc:
            raise SummarizerError(f"{self.name} output did not match the schema: {exc}") from exc


def _strip_fence(text: str) -> str:
    """Some models wrap JSON in ```json fences even in JSON mode."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()
