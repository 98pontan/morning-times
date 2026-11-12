"""Runtime settings: config.yaml, overridden by environment variables.

Everything that users edit in the frontend (sections, sources, stocks, teams,
regions) lives in the database instead; seed.yaml only fills an empty database.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

ENV_PREFIX = "APP_"


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class DatabaseSettings(BaseModel):
    # Overridden by the DATABASE_URL environment variable.
    url: str = "sqlite:///./morning-times.db"
    # Run Alembic migrations at startup (fine with a single replica).
    migrate_on_startup: bool = True


class SchedulerSettings(BaseModel):
    enabled: bool = True
    timezone: str = "Europe/Stockholm"
    ingest_interval_minutes: int = 15
    alerts_interval_minutes: int = 30
    # Local time (HH:MM) when the morning brief is generated and emailed.
    brief_time: str = "06:30"


class EmailSettings(BaseModel):
    enabled: bool = True
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    sender: str = ""
    recipients: list[str] = Field(default_factory=list)
    # Environment variable holding the SMTP (app) password.
    password_env: str = "EMAIL_PASSWORD"


class AISettings(BaseModel):
    # auto | claude | gemini | rules. auto picks the first provider whose API key is set (see src/ai).
    provider: Literal["auto", "claude", "gemini", "rules"] = "auto"
    claude_model: str = "claude-opus-5-5"
    gemini_model: str = "gemini-3.8-flash"
    brief_effort: str = "medium"
    rating_effort: str = "low"
    # Articles about watched stocks/teams/regions rated at or above this (1-10) trigger an alert email.
    alert_threshold: int = 7
    # How many recent articles per section are sent to Claude when writing the brief.
    max_articles_per_section: int = 40
    language: str = "Swedish"


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    ai: AISettings = Field(default_factory=AISettings)
    seed_file: str = "seed.yaml"

    model_config = {"extra": "forbid"}

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to show in the API: secrets reported only as present/absent."""
        data = self.model_dump()
        data["database"]["url"] = _mask_url(self.database.url)
        data["email"]["password_available"] = bool(os.environ.get(self.email.password_env))
        data["ai"]["anthropic_api_key_available"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
        data["ai"]["gemini_api_key_available"] = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        return data


def _mask_url(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def load_settings(path: str | Path | None, environ: dict[str, str] | None = None) -> Settings:
    environ = os.environ if environ is None else environ
    raw: dict[str, Any] = {}
    if path and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}
    elif path:
        raise FileNotFoundError(f"Config file not found: {path}")

    # APP_<SECTION>__<KEY>=value, e.g. APP_SCHEDULER__BRIEF_TIME=07:00
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX) or "__" not in name:
            continue
        section, _, key = name[len(ENV_PREFIX):].lower().partition("__")
        raw.setdefault(section, {})[key] = yaml.safe_load(value) if value else value

    if environ.get("DATABASE_URL"):
        raw.setdefault("database", {})["url"] = environ["DATABASE_URL"]
    return Settings.model_validate(raw)
