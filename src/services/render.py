"""HTML and plain-text rendering for emails (and later the frontend)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.ai import BriefContent

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


class Renderer:
    def __init__(self):
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))

    def brief_html(self, day: date, brief: BriefContent, articles: dict[int, dict],
                   section_names: dict[str, str], generator: str) -> str:
        note = "with Claude" if generator == "claude" else "from headlines (AI summaries are off)"
        return self.env.get_template("brief.html").render(
            day=day.isoformat(), brief=brief, articles=articles,
            section_names=section_names, generator_note=note,
        )

    @staticmethod
    def brief_text(day: date, brief: BriefContent, articles: dict[int, dict]) -> str:
        lines = [f"The Morning Times · {day.isoformat()}", "", brief.headline, ""]
        if brief.intro:
            lines += [brief.intro, ""]
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
