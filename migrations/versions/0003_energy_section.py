"""energy section: add "El" (spot prices, production, nuclear status, weekly outlook) to seeded databases

Like 0002: an empty database is left alone, seeding (after migrations) adds the section from seed.yaml.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

sections = sa.table(
    "sections",
    sa.column("id", sa.Integer), sa.column("slug", sa.String), sa.column("name", sa.String),
    sa.column("kind", sa.String), sa.column("enabled", sa.Boolean), sa.column("position", sa.Integer),
    sa.column("settings", sa.JSON),
)


def upgrade() -> None:
    conn = op.get_bind()
    if not conn.scalar(sa.select(sa.func.count()).select_from(sections)):
        return  # empty database: seed.yaml adds the section
    if conn.scalar(sa.select(sections.c.id).where(sections.c.kind == "energy").limit(1)):
        return
    slug = "el"
    if conn.scalar(sa.select(sections.c.id).where(sections.c.slug == slug)):
        slug = "elpriser"
    position = (conn.scalar(sa.select(sa.func.max(sections.c.position))) or 0) + 1
    conn.execute(sections.insert().values(
        slug=slug, name="El", kind="energy", enabled=True, position=position, settings={"zone": "SE3"},
    ))


def downgrade() -> None:
    pass  # data only; delete the section through the API if it is not wanted
