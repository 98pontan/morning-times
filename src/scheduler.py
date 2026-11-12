"""Background jobs: fetch articles, check alerts, send the morning brief."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from src.settings import SchedulerSettings

log = logging.getLogger(__name__)


class JobScheduler:
    def __init__(self, settings: SchedulerSettings, ingest, alerts, brief):
        self.settings = settings
        tz = ZoneInfo(settings.timezone)
        self.scheduler = BackgroundScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1})
        hour, minute = (int(x) for x in settings.brief_time.split(":"))
        now = datetime.now(tz)
        self.scheduler.add_job(ingest.run, "interval", id="ingest", next_run_time=now,
                               minutes=settings.ingest_interval_minutes)
        self.scheduler.add_job(alerts.run, "interval", id="alerts",
                               minutes=settings.alerts_interval_minutes)
        self.scheduler.add_job(brief.generate, "cron", id="brief", hour=hour, minute=minute,
                               misfire_grace_time=3600)

    def start(self) -> None:
        self.scheduler.start()
        log.info("Scheduler started: %s", ", ".join(f"{j.id} next {j.next_run_time:%Y-%m-%d %H:%M}"
                                                     for j in self.scheduler.get_jobs()))

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return self.scheduler.running

    def jobs(self) -> list[dict]:
        return [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
                for j in self.scheduler.get_jobs()]
