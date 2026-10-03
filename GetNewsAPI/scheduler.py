# scheduler.py


"""
Development-only APScheduler compatibility orchestration.

Starts the fetcher scheduler and runs a chained processor/publisher job on an interval.
The chained job processes stored news with GPT and then publishes the results to WordPress.
Production rejects ENABLE_APSCHEDULER=true and delegates recurrence to an external scheduler.

Side effects:
- Spawns background scheduler threads via APScheduler.
- Produces log output via the root logging configuration.
"""

import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

from fetcher import start_scheduler as start_fetcher_scheduler
from fetcher import stop_scheduler as stop_fetcher_scheduler
from operations.jobs import render_job_result, run_job
from runtime.logging_config import configure_logging


configure_logging()


_scheduler = None
_fetch_scheduler = None
_scheduler_lock = threading.Lock()


def chained_job():
    """Run the repository-owned process-then-publish one-shot contract."""

    result = run_job("pipeline_once")
    rendered = render_job_result(result)
    if result.exit_code:
        logging.error("Legacy APScheduler pipeline one-shot failed result=%s", rendered)
    else:
        logging.info("Legacy APScheduler pipeline one-shot finished result=%s", rendered)
    return result

        
def start_scheduler():
    """
    Start background schedulers for fetching and for the chained processing pipeline.

    Starts:
        - Fetcher scheduler: runs immediately and then every 30 minutes (implemented
          inside `fetcher.start_scheduler`).
        - Chained job scheduler: runs `chained_job()` every 30 minutes, with the first
          run delayed by 3 minutes to give the fetcher time to populate data.

    Scheduling details (chained job):
        - interval: 30 minutes
        - first run: now + 3 minutes
        - misfire_grace_time: 3600 seconds
        - coalesce: True (merge missed runs into one)
        - max_instances: 1 (prevent overlap)
        - jitter: 10 seconds

    Args:
        None

    Returns:
        tuple:
            References to the fetch and chained schedulers.
    """
    global _scheduler, _fetch_scheduler

    with _scheduler_lock:
        if _scheduler is not None:
            return _fetch_scheduler, _scheduler

        fetch_scheduler = None
        scheduler = None
        try:
            fetch_scheduler = start_fetcher_scheduler()
            # Run processor/publisher every 30 min, offset so fetch finishes first.
            scheduler = BackgroundScheduler()
            scheduler.add_job(
                chained_job,
                "interval",
                minutes=30,
                next_run_time=datetime.now() + timedelta(minutes=3),
                misfire_grace_time=3600,
                coalesce=True,
                max_instances=1,
                jitter=10
            )
            scheduler.start()
        except Exception:
            try:
                if scheduler is not None and scheduler.running:
                    scheduler.shutdown(wait=False)
            finally:
                stop_fetcher_scheduler(wait=False)
            raise

        _fetch_scheduler = fetch_scheduler
        _scheduler = scheduler
        return _fetch_scheduler, _scheduler


def stop_scheduler(wait: bool = False) -> None:
    """Stop both scheduler instances and clear their process-local references."""
    global _scheduler, _fetch_scheduler

    with _scheduler_lock:
        scheduler = _scheduler
        _scheduler = None
        _fetch_scheduler = None

    try:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=wait)
    finally:
        stop_fetcher_scheduler(wait=wait)
