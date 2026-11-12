"""HTML and plain-text rendering for emails (and later the frontend)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.ai import BriefContent
from src.sections.weather import local_text

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


class Renderer:
    def __init__(self):
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))

    def brief_html(self, day: date, brief: BriefContent, articles: dict[int, dict],
                   section_names: dict[str, str], generator: str, weather: dict | None = None) -> str:
        note = "with Claude" if generator == "claude" else "from headlines (AI summaries are off)"
        return self.env.get_template("brief.html").render(
            day=day.isoformat(), brief=brief, articles=articles,
            section_names=section_names, generator_note=note, weather=weather, local_text=_local_text,
            temp=_temp,
        )

    @staticmethod
    def brief_text(day: date, brief: BriefContent, articles: dict[int, dict], weather: dict | None = None) -> str:
        lines = [f"The Morning Times · {day.isoformat()}", "", brief.headline, ""]
        if brief.intro:
            lines += [brief.intro, ""]
        if weather and (weather["places"] or weather["warnings"]):
            lines += ["VÄDER", ""]
            for p in weather["places"]:
                if p["error"]:
                    continue
                lines.append(f"{p['name']}: {p['symbol_text']}, {_temp(p['min_temp'])} till {_temp(p['max_temp'])}, "
                             f"{p['precipitation'] or 0:.1f} mm")
            for w in weather["warnings"]:
                lines.append(f"! {w['level_name']}: {w['event']}, {w['area_name']} "
                             f"({_local_text(w['start'])} – {_local_text(w['end'])})")
            lines.append("")
        for section in brief.sections:
            lines += [section.title.upper(), ""]
            if section.summary:
                lines += [section.summary, ""]
            for h in section.highlights:
                a = articles.get(h.article_id)
                if a:
                    lines.append(f"- {h.text} ({a['url']})")
            lines.append("")
        return "\n".join(lines)

    def alerts_html(self, subject: str, alerts: list) -> str:
        return self.env.get_template("alerts.html").render(subject=subject, alerts=alerts)


def _temp(value: float | None) -> str:
    return "–" if value is None else f"{round(value):d}°"


def _local_text(value: str | None) -> str:
    return local_text(datetime.fromisoformat(value)) if value else "?"
