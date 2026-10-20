"""APScheduler wiring for quota-aware ingestion and weekly model retraining."""

from __future__ import annotations

import logging
import subprocess
import sys
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor

from accalab.config import ROOT

logger = logging.getLogger(__name__)


def _scheduler_config(path: Path | None = None) -> dict:
    source = path or ROOT / "config" / "strong_model.toml"
    return tomllib.loads(source.read_text(encoding="utf-8"))["scheduler"]


def run_cli_job(command: str) -> int:
    """Run one of the normal CLI commands with the same environment and repository root."""
    allowed = {
        "ingest-daily", "ingest-results", "retrain-model", "reconcile-payments", "send-notifications"
    }
    if command not in allowed:
        raise ValueError(f"Unsupported scheduled job: {command}")
    logger.info("Starting scheduled AccaLab job: %s", command)
    completed = subprocess.run(
        [sys.executable, "-m", "accalab.cli", command],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode:
        logger.error("Scheduled job %s exited with status %s", command, completed.returncode)
    else:
        logger.info("Scheduled job %s completed", command)
    return completed.returncode


def build_scheduler(config_path: Path | None = None) -> BackgroundScheduler:
    config = _scheduler_config(config_path)
    zone = ZoneInfo(config["timezone"])
    scheduler = BackgroundScheduler(
        timezone=zone,
        executors={"default": APSchedulerThreadPoolExecutor(1)},
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    scheduler.add_job(
        run_cli_job,
        "cron",
        id="daily-ingestion",
        args=["ingest-daily"],
        hour=int(config["daily_ingest_hour"]),
        minute=0,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_cli_job,
        "cron",
        id="daily-results-and-settlement",
        args=["ingest-results"],
        hour=int(config["results_ingest_hour"]),
        minute=0,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_cli_job,
        "cron",
        id="weekly-model-retraining",
        args=["retrain-model"],
        day_of_week=str(config["weekly_retrain_day"]),
        hour=int(config["weekly_retrain_hour"]),
        minute=0,
        misfire_grace_time=6 * 3600,
    )
    scheduler.add_job(
        run_cli_job,
        "cron",
        id="daily-payment-reconciliation",
        args=["reconcile-payments"],
        hour=int(config["payments_reconcile_hour"]),
        minute=0,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_cli_job,
        "interval",
        id="notification-outbox",
        args=["send-notifications"],
        minutes=max(1, int(config.get("notifications_interval_minutes", 5))),
        misfire_grace_time=300,
    )
    return scheduler


def run_scheduler(config_path: Path | None = None) -> None:
    scheduler = build_scheduler(config_path)
    scheduler.start()
    logger.info("AccaLab scheduler started with %s jobs", len(scheduler.get_jobs()))
    try:
        import threading

        threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=True)
