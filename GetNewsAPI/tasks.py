"""Cron-safe command-line entry points for the news pipeline."""

from __future__ import annotations

import os
import sys

from runtime.logging_config import configure_logging


configure_logging()

USAGE = (
    "Usage: python tasks.py "
    "[fetch_once|pipeline_once|fetch|process|publish|chained|"
    "embedding_ingest [limit]|"
    "embedding_worker [limit]|embedding_backfill [source|generated] [limit]|"
    "job_catalog|config_check [web|fetch|process|pipeline|publish|embedding]|"
    "migration_plan [app|vector|all]|migration_check [app|vector|all]|"
    "migration_apply [app|vector|all] [--backup-confirmed] [--restore-tested] "
    "[--allow-disposable]|migration_verify [app|vector|all]|feature_readiness]"
)


def _run_operational_job(
    name: str,
    *,
    limit: int | None = None,
    source: str = "source",
) -> int:
    from operations.jobs import render_job_result, run_job

    result = run_job(name, limit=limit, source=source)
    print(render_job_result(result))
    return result.exit_code


def _runner_exit(value) -> int:
    """Keep patched/legacy task runners compatible while honoring real exit codes."""

    return value if isinstance(value, int) else 0


def run_fetch_once() -> int:
    return _run_operational_job("fetch_once")


def run_fetch() -> int:
    """Backward-compatible alias for the explicit one-shot fetch command."""

    return run_fetch_once()


def run_process() -> int:
    return _run_operational_job("process")


def run_publish() -> int:
    return _run_operational_job("publish")


def run_pipeline_once() -> int:
    return _run_operational_job("pipeline_once")


def run_chained() -> int:
    """Backward-compatible alias for the production process-then-publish job."""

    return run_pipeline_once()


def run_embedding_ingest(limit: int | None = None) -> int:
    return _run_operational_job("embedding_ingest", limit=limit)


def run_embedding_worker(limit: int | None = None) -> int:
    return _run_operational_job("embedding_worker", limit=limit)


def run_embedding_backfill(
    source: str = "source",
    limit: int | None = None,
) -> int:
    normalized = source.strip().lower()
    if normalized not in {
        "source",
        "source_article",
        "generated",
        "coincourier_generated",
    }:
        raise ValueError("embedding backfill source must be source or generated")
    return _run_operational_job(
        "embedding_backfill",
        limit=limit,
        source=normalized,
    )


def run_job_catalog() -> int:
    from operations.jobs import job_catalog_payload

    print(job_catalog_payload())
    return 0


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

    if command == "fetch_once":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return _runner_exit(run_fetch_once())

    if command == "pipeline_once":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return _runner_exit(run_pipeline_once())

    if command == "fetch":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return _runner_exit(run_fetch())

    if command == "process":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return _runner_exit(run_process())

    if command == "publish":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return _runner_exit(run_publish())

    if command == "chained":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return _runner_exit(run_chained())

    if command == "embedding_ingest":
        if len(sys.argv) > 3:
            print(USAGE, file=sys.stderr)
            return 2
        try:
            return _runner_exit(run_embedding_ingest(_optional_positive_int(2)))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if command == "embedding_worker":
        if len(sys.argv) > 3:
            print(USAGE, file=sys.stderr)
            return 2
        try:
            return _runner_exit(run_embedding_worker(_optional_positive_int(2)))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if command == "embedding_backfill":
        if len(sys.argv) > 4:
            print(USAGE, file=sys.stderr)
            return 2
        source = sys.argv[2] if len(sys.argv) > 2 else "source"
        try:
            return _runner_exit(
                run_embedding_backfill(source, _optional_positive_int(3))
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if command == "job_catalog":
        if len(sys.argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        return run_job_catalog()

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
