"""Cron-safe command-line entry points for the news pipeline."""

from __future__ import annotations

import logging
import sys


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

USAGE = "Usage: python tasks.py [fetch|process|publish|chained]"


def run_fetch() -> None:
    from fetcher import run_fetch_cycle

    logger.info("[TASK] fetch start")
    result = run_fetch_cycle()
    logger.info("[TASK] fetch done result=%s", result)


def run_process() -> None:
    from gpt_processor import process_news_with_gpt

    logger.info("[TASK] process start")
    result = process_news_with_gpt()
    logger.info("[TASK] process done result=%s", result)


def run_publish() -> None:
    from publish_to_wp import publish_news_to_wp

    logger.info("[TASK] publish start")
    result = publish_news_to_wp()
    logger.info("[TASK] publish done result=%s", result)


def run_chained() -> None:
    logger.info("[TASK] chained start")

    try:
        run_process()
    except Exception:
        logger.exception("[TASK] process failed during chained run; publish will still be attempted")

    try:
        run_publish()
    except Exception:
        logger.exception("[TASK] publish failed during chained run")

    logger.info("[TASK] chained done")


def main() -> int:
    command = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

    if command == "fetch":
        run_fetch()
        return 0

    if command == "process":
        run_process()
        return 0

    if command == "publish":
        run_publish()
        return 0

    if command == "chained":
        run_chained()
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
