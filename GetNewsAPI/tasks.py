"""Cron-safe command-line entry points for the news pipeline."""

from __future__ import annotations

import logging
import os
import sys

from runtime.logging_config import configure_logging


configure_logging()

logger = logging.getLogger(__name__)

USAGE = (
    "Usage: python tasks.py "
    "[fetch|process|publish|chained|embedding_ingest [limit]|"
    "embedding_worker [limit]|embedding_backfill [source|generated] [limit]|"
    "config_check [web|pipeline|publish|embedding]|"
    "migration_plan [app|vector|all]|migration_check [app|vector|all]|"
    "migration_apply [app|vector|all] [--backup-confirmed] [--restore-tested] "
    "[--allow-disposable]|migration_verify [app|vector|all]|feature_readiness]"
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


def run_config_check(profile: str = "web") -> bool:
    """Validate environment configuration without opening external connections."""

    from runtime.config_validation import (
        format_config_issues,
        validate_runtime_config,
    )

    environment = dict(os.environ)
    issues = validate_runtime_config(environment, profile=profile)
    if not issues:
        print(f"Configuration valid for profile: {profile}")
        return True

    rendered = format_config_issues(issues)
    print(f"Configuration invalid for profile: {profile}", file=sys.stderr)
    print(rendered, file=sys.stderr)
    return False


def _optional_positive_int(index: int) -> int | None:
    if len(sys.argv) <= index:
        return None
    value = int(sys.argv[index])
    if value <= 0:
        raise ValueError("task limit must be positive")
    return value


def _migration_targets(value: str):
    from deployment.models import MigrationTarget

    normalized = value.strip().lower()
    if normalized == "all":
        return (MigrationTarget.APP, MigrationTarget.VECTOR)
    try:
        return (MigrationTarget(normalized),)
    except ValueError as exc:
        raise ValueError("migration target must be app, vector, or all") from exc


def run_migration_command(
    operation: str,
    target_value: str,
    *,
    backup_confirmed: bool = False,
    restore_tested: bool = False,
    allow_disposable: bool = False,
) -> bool:
    from deployment.service import MigrationService, render_result

    service = MigrationService()
    success = True
    for target in _migration_targets(target_value):
        if operation == "migration_apply":
            result = service.apply(
                target,
                backup_confirmed=backup_confirmed,
                restore_tested=restore_tested,
                allow_disposable=allow_disposable,
            )
        else:
            result = getattr(service, operation.removeprefix("migration_"))(target)
        print(render_result(result))
        success = success and result.success
        if not result.success and operation == "migration_apply":
            break
    return success


def run_feature_readiness() -> bool:
    from deployment.service import MigrationService, render_readiness

    statuses = MigrationService().feature_readiness()
    print(render_readiness(statuses))
    return all(status.ready for status in statuses)


def main() -> int:
    command = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

    if command in {"help", "-h", "--help"}:
        print(USAGE)
        return 0

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

    if command == "config_check":
        profile = sys.argv[2] if len(sys.argv) > 2 else "web"
        return 0 if run_config_check(profile) else 1

    if command in {"migration_plan", "migration_check", "migration_verify"}:
        if len(sys.argv) > 3:
            print(USAGE, file=sys.stderr)
            return 2
        target = sys.argv[2] if len(sys.argv) > 2 else "all"
        try:
            return 0 if run_migration_command(command, target) else 1
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if command == "migration_apply":
        target = sys.argv[2] if len(sys.argv) > 2 else "all"
        flags = set(sys.argv[3:])
        allowed = {"--backup-confirmed", "--restore-tested", "--allow-disposable"}
        if flags - allowed or len(flags) != len(sys.argv[3:]):
            print(USAGE, file=sys.stderr)
            return 2
        try:
            success = run_migration_command(
                command,
                target,
                backup_confirmed="--backup-confirmed" in flags,
                restore_tested="--restore-tested" in flags,
                allow_disposable="--allow-disposable" in flags,
            )
            return 0 if success else 1
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if command == "feature_readiness":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return 0 if run_feature_readiness() else 1

    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
