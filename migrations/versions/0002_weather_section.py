"""weather section: add "Väder" (SMHI warnings for Stockholms län + Stockholm forecast) to seeded databases

seed.yaml only fills an empty database, so a database seeded before the weather section existed gets
it here. An empty database is left alone: seeding (which runs after migrations) adds it from seed.yaml.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

sections = sa.table(
    "sections",
    sa.column("id", sa.Integer), sa.column("slug", sa.String), sa.column("name", sa.String),
    sa.column("kind", sa.String), sa.column("enabled", sa.Boolean), sa.column("position", sa.Integer),
    sa.column("settings", sa.JSON),
)
watch_items = sa.table(
    "watch_items",
    sa.column("section_id", sa.Integer), sa.column("type", sa.String), sa.column("name", sa.String),
    sa.column("symbol", sa.String), sa.column("keywords", sa.JSON), sa.column("settings", sa.JSON),
    sa.column("enabled", sa.Boolean),
)


def upgrade() -> None:
    conn = op.get_bind()
    if not conn.scalar(sa.select(sa.func.count()).select_from(sections)):
        return  # empty database: seed.yaml adds the section
    if conn.scalar(sa.select(sections.c.id).where(sections.c.kind == "weather").limit(1)):
        return
    slug = "vader"
    if conn.scalar(sa.select(sections.c.id).where(sections.c.slug == slug)):
        slug = "vader-smhi"
    position = (conn.scalar(sa.select(sa.func.max(sections.c.position))) or 0) + 1
    section_id = conn.execute(sections.insert().values(
        slug=slug, name="Väder", kind="weather", enabled=True, position=position,
        settings={"warning_levels": ["YELLOW", "ORANGE", "RED"]},
    ).returning(sections.c.id)).scalar_one()
    conn.execute(watch_items.insert(), [
        {"section_id": section_id, "type": "warning_area", "name": "Stockholms län", "symbol": "1",
         "keywords": [], "settings": {"levels": ["YELLOW", "ORANGE"]}, "enabled": True},
        {"section_id": section_id, "type": "place", "name": "Stockholm", "symbol": "59.3293,18.0686",
         "keywords": [], "settings": {}, "enabled": True},
    ])


def downgrade() -> None:
    pass  # data only; delete the section through the API if it is not wanted
