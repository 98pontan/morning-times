"""Engine, sessions and migrations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Database:
    def __init__(self, url: str):
        self.url = url
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            # SQLite ignores foreign keys (and ON DELETE CASCADE) unless asked.
            event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope for background jobs: commit on success, roll back on error."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def migrate(self) -> None:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        cfg.attributes["skip_logging"] = True  # keep the app's logging config
        command.upgrade(cfg, "head")
