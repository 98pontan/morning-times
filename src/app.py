"""Wires the components together. Add or remove features here."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from src.ai import Summarizer, build_summarizer
from src.api.routes import router
from src.db.database import Database
from src.notifier import Notifier, build_notifier
from src.scheduler import JobScheduler
from src.seed import seed_if_empty
from src.services.alerts import AlertService
from src.services.brief import BriefService
from src.services.ingest import IngestService
from src.services.render import Renderer
from src.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    summarizer: Summarizer
    notifier: Notifier
    ingest: IngestService
    alerts: AlertService
    brief: BriefService
    scheduler: JobScheduler | None = None

    @classmethod
    def build(cls, settings: Settings, summarizer: Summarizer | None = None,
              notifier: Notifier | None = None) -> "Services":
        db = Database(settings.database.url)
        summarizer = summarizer or build_summarizer(settings.ai)
        notifier = notifier or build_notifier(settings.email)
        renderer = Renderer()
        return cls(
            settings=settings, db=db, summarizer=summarizer, notifier=notifier,
            ingest=IngestService(db),
            alerts=AlertService(db, summarizer, notifier, settings.ai, renderer),
            brief=BriefService(db, summarizer, notifier, settings, renderer),
        )


def create_app(settings: Settings, services: Services | None = None) -> FastAPI:
    services = services or Services.build(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.database.migrate_on_startup:
            services.db.migrate()
        seed_if_empty(services.db, settings.seed_file)
        if settings.scheduler.enabled:
            services.scheduler = JobScheduler(
                settings.scheduler, services.ingest, services.alerts, services.brief
            )
            services.scheduler.start()
        log.info("Morning Times started (summarizer: %s)", services.summarizer.name)
        yield
        if services.scheduler:
            services.scheduler.shutdown()

    app = FastAPI(
        title="The Morning Times",
        description="Configurable news aggregator with a daily AI-written morning brief.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    app.include_router(router)
    return app
