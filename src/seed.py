"""Fill an empty database from seed.yaml. After that, the database (and the frontend) owns the config."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import func, select

from src.db.database import Database
from src.db.models import Section, Source, WatchItem
from src.sections import get_plugin

log = logging.getLogger(__name__)


def seed_if_empty(db: Database, path: str | Path) -> bool:
    path = Path(path)
    with db.session() as session:
        if session.scalar(select(func.count(Section.id))):
            return False
        if not path.exists():
            log.warning("Database is empty and seed file %s does not exist", path)
            return False
        data = yaml.safe_load(path.read_text()) or {}
        for position, s in enumerate(data.get("sections", [])):
            plugin = get_plugin(s["kind"])
            section = Section(
                slug=s["slug"], name=s["name"], kind=s["kind"], position=position,
                enabled=s.get("enabled", True), settings=s.get("settings", {}),
            )
            section.sources = [
                Source(name=src["name"], url=src["url"], kind=src.get("kind", "rss")) for src in s.get("sources", [])
            ]
            for item in s.get("watch", []):
                if item["type"] not in plugin.item_types:
                    raise ValueError(f"Section {s['slug']} ({s['kind']}) cannot watch type {item['type']}")
                section.watch_items.append(WatchItem(
                    type=item["type"], name=item["name"], symbol=item.get("symbol"),
                    keywords=item.get("keywords", []), settings=item.get("settings", {}),
                ))
            session.add(section)
        log.info("Seeded database from %s", path)
        return True
