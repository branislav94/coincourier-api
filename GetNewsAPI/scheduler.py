# scheduler.py


"""
APScheduler orchestration.

Starts the fetcher scheduler and runs a chained processor/publisher job on an interval.
The chained job processes stored news with GPT and then publishes the results to WordPress.

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
from gpt_processor import process_news_with_gpt
from publish_to_wp import publish_news_to_wp


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


_scheduler = None
_fetch_scheduler = None
_scheduler_lock = threading.Lock()


def chained_job():
    """
    Run the processing and publishing pipeline in sequence.

    Executes:
        1) `process_news_with_gpt()` to transform/prepare stored news content.
        2) `publish_news_to_wp()` to publish prepared items to WordPress.

    Behavior:
        - Logs start/end markers for each stage.
        - Catches and logs exceptions for each stage independently so a failure in
          processing does not prevent the publish stage from attempting to run.

    Args:
        None

    Returns:
        None
    """
    logging.info("▶ starting chained job")
    logging.info("Starting process_news_with_gpt job.")
    try:
        result = process_news_with_gpt()
        attempted = int((result or {}).get("attempted", 0))
        succeeded = int((result or {}).get("succeeded", 0))
        failed = int((result or {}).get("failed", 0))
        if attempted == 0:
            logging.info("Finished processing news with GPT: no eligible articles.")
        elif failed == 0:
            logging.info("Finished processing news with GPT successfully: %s", result)
        elif succeeded == 0:
            logging.error("Finished processing news with GPT: all attempted articles failed: %s", result)
        else:
            logging.warning("Finished processing news with GPT with partial failures: %s", result)
    except Exception:
        logging.exception("Error during process_news_with_gpt")

    logging.info("Starting publish_news_to_wp job.")
    try:
        publish_news_to_wp()
        logging.info("Finished publishing news to WordPress successfully.")
    except Exception:
        logging.exception("Error during publish_news_to_wp")

        
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
