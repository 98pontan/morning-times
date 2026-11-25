"""users: Keycloak users, personal watches (watch_items.owner_id) and per-user alerts (alerts.user_id)

Existing watches keep owner_id NULL and become the defaults that visitors see and new users start from.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False, unique=True),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("email", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    # batch mode so SQLite (local development) can add the foreign keys too
    with op.batch_alter_table("watch_items") as batch:
        batch.add_column(sa.Column("owner_id", sa.Integer()))
        batch.create_foreign_key("fk_watch_items_owner", "users", ["owner_id"], ["id"], ondelete="CASCADE")
        batch.create_index("ix_watch_items_owner_id", ["owner_id"])
    with op.batch_alter_table("alerts") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer()))
        batch.create_foreign_key("fk_alerts_user", "users", ["user_id"], ["id"], ondelete="CASCADE")
        batch.create_index("ix_alerts_user_id", ["user_id"])


def downgrade() -> None:
    with op.batch_alter_table("alerts") as batch:
        batch.drop_index("ix_alerts_user_id")
        batch.drop_constraint("fk_alerts_user", type_="foreignkey")
        batch.drop_column("user_id")
    with op.batch_alter_table("watch_items") as batch:
        batch.drop_index("ix_watch_items_owner_id")
        batch.drop_constraint("fk_watch_items_owner", type_="foreignkey")
        batch.drop_column("owner_id")
    op.drop_table("users")
