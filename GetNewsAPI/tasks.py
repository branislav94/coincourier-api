"""Cron-safe command-line entry points for the news pipeline."""

from __future__ import annotations

import logging
import sys


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

USAGE = (
    "Usage: python tasks.py "
    "[fetch|process|publish|chained|embedding_ingest [limit]|"
    "embedding_worker [limit]|embedding_backfill [source|generated] [limit]]"
)


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


def run_embedding_ingest(limit: int | None = None):
    from embeddings.operations import run_embedding_ingest as run

    logger.info("[TASK] embedding_ingest start limit=%s", limit)
    result = run(limit=limit)
    logger.info("[TASK] embedding_ingest done result=%s", result)
    return result


def run_embedding_worker(limit: int | None = None):
    from embeddings.operations import run_embedding_worker as run

    logger.info("[TASK] embedding_worker start limit=%s", limit)
    result = run(limit=limit)
    logger.info("[TASK] embedding_worker done result=%s", result)
    return result


def run_embedding_backfill(
    source: str = "source",
    limit: int | None = None,
):
    from embeddings.operations import run_embedding_backfill as run
    from vector_store.models import SourceType

    normalized = source.strip().lower()
    source_types = {
        "source": SourceType.SOURCE_ARTICLE,
        "source_article": SourceType.SOURCE_ARTICLE,
        "generated": SourceType.COINCOURIER_GENERATED,
        "coincourier_generated": SourceType.COINCOURIER_GENERATED,
    }
    if normalized not in source_types:
        raise ValueError("embedding backfill source must be source or generated")
    logger.info(
        "[TASK] embedding_backfill start source=%s limit=%s",
        normalized,
        limit,
    )
    result = run(source_types[normalized], limit=limit)
    logger.info("[TASK] embedding_backfill done result=%s", result)
    return result


def _optional_positive_int(index: int) -> int | None:
    if len(sys.argv) <= index:
        return None
    value = int(sys.argv[index])
    if value <= 0:
        raise ValueError("task limit must be positive")
    return value


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

    if command == "embedding_ingest":
        run_embedding_ingest(_optional_positive_int(2))
        return 0

    if command == "embedding_worker":
        run_embedding_worker(_optional_positive_int(2))
        return 0

    if command == "embedding_backfill":
        source = sys.argv[2] if len(sys.argv) > 2 else "source"
        run_embedding_backfill(source, _optional_positive_int(3))
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
