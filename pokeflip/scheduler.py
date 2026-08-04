"""The scheduled half of the app.

Three jobs do the work:

* **refresh** - pull fresh prices for everything tracked, then re-score and
  fire any alerts that trip.
* **digest** - build and deliver the daily (and weekly) report.
* **catalog** - pick up new sets and new printings.

Run it with ``pokeflip run`` (scheduler only) or ``pokeflip serve`` (scheduler
plus the web UI and API in one process).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import digest as digest_mod
from . import ingest, notify, signals
from .alerts import evaluate_alerts, mark_delivered, store_alerts
from .config import Config
from .db import Database
from .providers import build_provider

log = logging.getLogger("pokeflip.scheduler")


def run_refresh_cycle(db: Database, config: Config, deliver: bool = True
                      ) -> dict[str, Any]:
    """Fetch prices, re-score, raise alerts. The core periodic job."""
    stats = ingest.refresh_prices(db, config)
    run = signals.generate(db, config)
    found = evaluate_alerts(db, config, run=run)
    stored = store_alerts(db, found)

    delivery: list[dict[str, Any]] = []
    if stored and deliver:
        delivery = notify.deliver_alerts(config, stored)
        if any(d.get("ok") for d in delivery):
            mark_delivered(db, [a["id"] for a in stored])

    result = {
        "prices": stats,
        "buys": len(run.buys),
        "sells": len(run.sells),
        "scanned": run.scanned,
        "alerts": len(stored),
        "delivery": delivery,
    }
    log.info(
        "refresh: %s quotes, %s buys, %s sells, %s alerts",
        stats.get("quotes", 0), len(run.buys), len(run.sells), len(stored),
    )
    return result


def run_digest(db: Database, config: Config, kind: str = "daily",
               deliver: bool = True, today: date | None = None) -> dict[str, Any]:
    """Build, save and deliver a digest."""
    built = digest_mod.build(db, config, kind=kind, today=today)
    paths = digest_mod.save(db, config, built)
    delivery = notify.deliver_digest(config, built, paths) if deliver else []
    log.info("digest(%s): %s", kind, built.headline)
    return {
        "kind": kind,
        "headline": built.headline,
        "paths": paths,
        "delivery": delivery,
        "digest": built.to_dict(),
    }


def run_catalog_sync(db: Database, config: Config) -> dict[str, Any]:
    """Refresh set metadata, and the card list of every tracked set."""
    provider = build_provider(config)
    try:
        sets = ingest.sync_sets(db, provider)
        synced = []
        for set_id in config.tracked_sets:
            synced.append(ingest.sync_set_cards(db, config, set_id, provider=provider))
        log.info("catalog: %s sets, %s tracked sets refreshed", sets, len(synced))
        return {"sets": sets, "tracked": synced}
    finally:
        provider.close()


class Scheduler:
    """Owns the background jobs. Safe to start once per process."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self._scheduler = BackgroundScheduler(timezone=config.timezone)
        self._configured = False

    def configure(self) -> None:
        if self._configured:
            return
        rules = self.config.schedule

        self._add(
            "refresh",
            IntervalTrigger(minutes=max(5, rules.refresh_interval_minutes)),
            lambda: run_refresh_cycle(self.db, self.config),
        )
        self._add(
            "digest-daily",
            CronTrigger(hour=rules.digest_hour, minute=rules.digest_minute),
            lambda: run_digest(self.db, self.config, "daily"),
        )
        self._add(
            "digest-weekly",
            CronTrigger(day_of_week=rules.weekly_digest_day, hour=rules.weekly_digest_hour,
                        minute=rules.digest_minute),
            lambda: run_digest(self.db, self.config, "weekly"),
        )
        self._add(
            "catalog",
            IntervalTrigger(days=max(1, rules.catalog_refresh_days)),
            lambda: run_catalog_sync(self.db, self.config),
        )
        self._configured = True

    def _add(self, job_id: str, trigger: Any, func: Callable[[], Any]) -> None:
        self._scheduler.add_job(
            _guarded(job_id, func),
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            # A missed window should still run rather than silently vanish, but
            # only one instance at a time - refreshes can outlast their interval.
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    def start(self) -> None:
        if not self.config.schedule.enabled:
            log.info("scheduler disabled by config")
            return
        self.configure()
        self._scheduler.start()
        log.info(
            "scheduler started: refresh every %s min, digest at %02d:%02d %s",
            self.config.schedule.refresh_interval_minutes,
            self.config.schedule.digest_hour,
            self.config.schedule.digest_minute,
            self.config.timezone,
        )
        if self.config.schedule.run_on_start:
            self._scheduler.add_job(
                _guarded("startup", lambda: self._startup_cycle()),
                id="startup", replace_existing=True,
            )

    def _startup_cycle(self) -> None:
        run_refresh_cycle(self.db, self.config)
        run_digest(self.db, self.config, "daily")

    def shutdown(self, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    @property
    def running(self) -> bool:
        return self._scheduler.running

    def jobs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self._scheduler.get_jobs()
        ]


def _guarded(job_id: str, func: Callable[[], Any]) -> Callable[[], Any]:
    """A job that raises must not take the scheduler thread down with it."""
    def wrapper() -> Any:
        try:
            return func()
        except Exception:
            log.exception("scheduled job %s failed", job_id)
            return None
    wrapper.__name__ = f"job_{job_id.replace('-', '_')}"
    return wrapper
