"""Explicit catalog and execution service for one-shot operational jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from typing import Any, Callable, Mapping

from runtime.config_validation import FALSE_VALUES, TRUE_VALUES, validate_runtime_config

from .locking import ConnectionFactory, advisory_job_lock
from .models import (
    DatabaseTarget,
    JobResult,
    JobSpec,
    JobStatus,
    ScheduleClass,
    WorkResult,
)


@dataclass(frozen=True)
class JobRequest:
    limit: int | None = None
    source: str = "source"


JOB_CATALOG: tuple[JobSpec, ...] = (
    JobSpec(
        name="fetch_once",
        description="Fetch, score, persist, and schedule one bounded candidate pool.",
        command="python tasks.py fetch_once",
        executor="fetch",
        schedule_class=ScheduleClass.RECURRING,
        config_profile="fetch",
        conflict_group="fetch",
        lock_target=DatabaseTarget.APP,
        lock_strategy="existing:news_fetcher_lock",
        parallel_safe=False,
        bounds=("FETCH_POOL_SIZE", "FETCH_SCORE_LIMIT", "ITEMS_PER_PULL=100"),
        cadence="every 30 minutes; initial run immediate",
    ),
    JobSpec(
        name="pipeline_once",
        description="Process one bounded due batch, then publish one bounded due batch.",
        command="python tasks.py pipeline_once",
        executor="pipeline",
        schedule_class=ScheduleClass.RECURRING,
        config_profile="pipeline",
        conflict_group="pipeline-workflow",
        lock_target=DatabaseTarget.APP,
        lock_strategy="job_advisory",
        parallel_safe=False,
        bounds=("PROCESS_BATCH_MIN..PROCESS_BATCH_MAX", "PUBLISH_BATCH_MAX"),
        cadence="every 30 minutes; initial run about 3 minutes after fetch",
    ),
    JobSpec(
        name="process",
        description="Manually process one bounded due article batch.",
        command="python tasks.py process",
        executor="process",
        schedule_class=ScheduleClass.MANUAL,
        config_profile="process",
        conflict_group="pipeline-workflow",
        lock_target=DatabaseTarget.APP,
        lock_strategy="job_advisory",
        parallel_safe=False,
        bounds=("PROCESS_BATCH_MIN..PROCESS_BATCH_MAX",),
    ),
    JobSpec(
        name="publish",
        description="Manually publish one bounded due article batch.",
        command="python tasks.py publish",
        executor="publish",
        schedule_class=ScheduleClass.MANUAL,
        config_profile="publish",
        conflict_group="pipeline-workflow",
        lock_target=DatabaseTarget.APP,
        lock_strategy="job_advisory+existing:wp_publisher_lock",
        parallel_safe=False,
        bounds=("PUBLISH_BATCH_MAX",),
    ),
    JobSpec(
        name="embedding_ingest",
        description="Register recent source/generated documents and durable embedding jobs.",
        command="python tasks.py embedding_ingest [limit]",
        executor="embedding_ingest",
        schedule_class=ScheduleClass.ROLLOUT_DECISION,
        config_profile="embedding_ingest",
        conflict_group="embedding-registration",
        lock_target=DatabaseTarget.VECTOR,
        lock_strategy="job_advisory",
        parallel_safe=False,
        required_features=("VECTOR_ENABLED",),
        bounds=("EMBEDDING_INGEST_LIMIT", "maximum=1000"),
    ),
    JobSpec(
        name="embedding_worker",
        description="Claim and execute a bounded number of durable embedding jobs.",
        command="python tasks.py embedding_worker [limit]",
        executor="embedding_worker",
        schedule_class=ScheduleClass.ROLLOUT_DECISION,
        config_profile="embedding_worker",
        conflict_group=None,
        lock_target=DatabaseTarget.VECTOR,
        lock_strategy="durable_row_claims",
        parallel_safe=True,
        required_features=("VECTOR_ENABLED", "EMBEDDING_ENABLED"),
        bounds=("EMBEDDING_WORK_LIMIT", "maximum=100", "EMBEDDING_MAX_CHUNKS_PER_JOB"),
    ),
    JobSpec(
        name="embedding_backfill",
        description="Manually scan a bounded, deterministic historical source page sequence.",
        command="python tasks.py embedding_backfill [source|generated] [limit]",
        executor="embedding_backfill",
        schedule_class=ScheduleClass.MANUAL,
        config_profile="embedding_backfill",
        conflict_group="embedding-registration",
        lock_target=DatabaseTarget.VECTOR,
        lock_strategy="job_advisory",
        parallel_safe=False,
        required_features=("VECTOR_ENABLED",),
        bounds=("EMBEDDING_INGEST_LIMIT", "EMBEDDING_BACKFILL_PAGE_SIZE", "maximum=1000"),
    ),
)


def job_by_name(name: str) -> JobSpec:
    normalized = name.strip().lower()
    for job in JOB_CATALOG:
        if job.name == normalized:
            return job
    raise ValueError(f"unsupported operational job: {name}")


def _safe_feature_state(environment: Mapping[str, str], name: str) -> bool | None:
    raw = (environment.get(name) or "false").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES or not raw:
        return False
    return None


def _finished(
    spec: JobSpec,
    started_at: datetime,
    work: WorkResult,
    *,
    lock_name: str | None = None,
) -> JobResult:
    return JobResult(
        job_name=spec.name,
        status=work.status,
        stage=work.stage,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        counts=dict(work.counts),
        reason_type=work.reason_type,
        details=work.details,
        lock_name=lock_name,
    )


def _failed(stage: str, error: BaseException) -> WorkResult:
    return WorkResult(
        JobStatus.FAILED,
        stage,
        reason_type=type(error).__name__ or "Error",
    )


def _counter_mapping(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    source = asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)
    return {
        str(key): int(item)
        for key, item in source.items()
        if isinstance(item, (bool, int)) and key != "status"
    }


def _execute_fetch(_request: JobRequest) -> WorkResult:
    from fetcher import run_fetch_cycle

    result = run_fetch_cycle() or {"status": "success"}
    counts = _counter_mapping(result)
    status = str(result.get("status", "success")).lower()
    if status == "skipped_already_running":
        return WorkResult(JobStatus.SKIPPED_ALREADY_RUNNING, "fetch", counts)
    if status == "no_work":
        return WorkResult(JobStatus.NO_WORK, "fetch", counts)
    if status == "failed":
        return WorkResult(
            JobStatus.FAILED,
            "fetch",
            counts,
            str(result.get("reason_type") or "FetchError"),
        )
    return WorkResult(JobStatus.SUCCESS, "fetch", counts)


def _execute_process(_request: JobRequest) -> WorkResult:
    from gpt_processor import process_news_with_gpt

    counts = _counter_mapping(process_news_with_gpt())
    if counts.get("failed", 0):
        return WorkResult(JobStatus.FAILED, "process", counts, "ArticleFailures")
    status = JobStatus.NO_WORK if counts.get("attempted", 0) == 0 else JobStatus.SUCCESS
    return WorkResult(status, "process", counts)


def _execute_publish(_request: JobRequest) -> WorkResult:
    from publish_to_wp import publish_news_to_wp

    result = publish_news_to_wp() or {"status": "success"}
    counts = _counter_mapping(result)
    status = str(result.get("status", "success")).lower()
    if status == "skipped_already_running":
        return WorkResult(JobStatus.SKIPPED_ALREADY_RUNNING, "publish", counts)
    if status == "no_work":
        return WorkResult(JobStatus.NO_WORK, "publish", counts)
    if status == "failed" or counts.get("failed", 0):
        return WorkResult(JobStatus.FAILED, "publish", counts, "PublicationFailures")
    return WorkResult(JobStatus.SUCCESS, "publish", counts)


def _stage_call(stage: str, call: Callable[[], WorkResult]) -> WorkResult:
    try:
        return call()
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return _failed(stage, error)


def _execute_pipeline(request: JobRequest) -> WorkResult:
    process = _stage_call("process", lambda: _execute_process(request))
    publish = _stage_call("publish", lambda: _execute_publish(request))
    counts = {
        **{f"process_{key}": value for key, value in process.counts.items()},
        **{f"publish_{key}": value for key, value in publish.counts.items()},
    }
    failed = [result for result in (process, publish) if result.status is JobStatus.FAILED]
    if failed:
        return WorkResult(
            JobStatus.FAILED,
            ",".join(result.stage for result in failed),
            counts,
            ",".join(result.reason_type or "JobFailure" for result in failed),
        )
    skipped = [
        result
        for result in (process, publish)
        if result.status is JobStatus.SKIPPED_ALREADY_RUNNING
    ]
    if skipped:
        return WorkResult(
            JobStatus.SKIPPED_ALREADY_RUNNING,
            ",".join(result.stage for result in skipped),
            counts,
        )
    if all(result.status is JobStatus.NO_WORK for result in (process, publish)):
        return WorkResult(JobStatus.NO_WORK, "pipeline", counts)
    return WorkResult(JobStatus.SUCCESS, "pipeline", counts)


def _execute_embedding_ingest(request: JobRequest) -> WorkResult:
    from embeddings.operations import run_embedding_ingest

    metrics = run_embedding_ingest(limit=request.limit)
    counts = _counter_mapping(metrics)
    status = JobStatus.NO_WORK if metrics.documents_scanned == 0 else JobStatus.SUCCESS
    return WorkResult(status, "embedding_ingest", counts)


def _execute_embedding_worker(request: JobRequest) -> WorkResult:
    from embeddings.operations import run_embedding_worker

    metrics = run_embedding_worker(limit=request.limit)
    counts = _counter_mapping(metrics)
    if metrics.jobs_retryable or metrics.jobs_failed or metrics.jobs_lost_claim:
        return WorkResult(JobStatus.FAILED, "embedding_worker", counts, "EmbeddingJobFailures")
    status = JobStatus.NO_WORK if metrics.jobs_claimed == 0 else JobStatus.SUCCESS
    return WorkResult(status, "embedding_worker", counts)


def _execute_embedding_backfill(request: JobRequest) -> WorkResult:
    from embeddings.operations import run_embedding_backfill
    from vector_store.models import SourceType

    sources = {
        "source": SourceType.SOURCE_ARTICLE,
        "source_article": SourceType.SOURCE_ARTICLE,
        "generated": SourceType.COINCOURIER_GENERATED,
        "coincourier_generated": SourceType.COINCOURIER_GENERATED,
    }
    normalized = request.source.strip().lower()
    if normalized not in sources:
        raise ValueError("embedding backfill source must be source or generated")
    metrics = run_embedding_backfill(sources[normalized], limit=request.limit)
    counts = _counter_mapping(metrics)
    status = JobStatus.NO_WORK if metrics.documents_scanned == 0 else JobStatus.SUCCESS
    return WorkResult(status, "embedding_backfill", counts)


EXECUTORS: dict[str, Callable[[JobRequest], WorkResult]] = {
    "fetch": _execute_fetch,
    "pipeline": _execute_pipeline,
    "process": _execute_process,
    "publish": _execute_publish,
    "embedding_ingest": _execute_embedding_ingest,
    "embedding_worker": _execute_embedding_worker,
    "embedding_backfill": _execute_embedding_backfill,
}


def _lock_timeout(environment: Mapping[str, str]) -> int:
    try:
        timeout = int(environment.get("JOB_LOCK_TIMEOUT_SECONDS", "1"))
    except ValueError as error:
        raise ValueError("JOB_LOCK_TIMEOUT_SECONDS must be an integer") from error
    if not 1 <= timeout <= 60:
        raise ValueError("JOB_LOCK_TIMEOUT_SECONDS must be between 1 and 60")
    return timeout


def run_job(
    name: str,
    *,
    limit: int | None = None,
    source: str = "source",
    environment: Mapping[str, str] | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> JobResult:
    """Validate, lock where required, execute once, and always terminate."""

    spec = job_by_name(name)
    started_at = datetime.now(timezone.utc)
    env = dict(os.environ if environment is None else environment)
    disabled = [
        feature
        for feature in spec.required_features
        if _safe_feature_state(env, feature) is False
    ]
    if disabled:
        return _finished(
            spec,
            started_at,
            WorkResult(
                JobStatus.DISABLED,
                "configuration",
                reason_type="FeatureDisabled",
                details=tuple(f"{feature}=false" for feature in disabled),
            ),
        )

    issues = validate_runtime_config(env, profile=spec.config_profile)
    if issues:
        return _finished(
            spec,
            started_at,
            WorkResult(
                JobStatus.CONFIGURATION_ERROR,
                "configuration",
                reason_type="ConfigurationError",
                details=tuple(issue.render() for issue in issues),
            ),
        )

    request = JobRequest(limit=limit, source=source)
    executor = EXECUTORS[spec.executor]
    try:
        if spec.lock_strategy.startswith("job_advisory"):
            assert spec.conflict_group is not None and spec.lock_target is not None
            with advisory_job_lock(
                spec.conflict_group,
                spec.lock_target,
                environment=env,
                timeout_seconds=_lock_timeout(env),
                connection_factory=connection_factory,
            ) as lock:
                if not lock.acquired:
                    return _finished(
                        spec,
                        started_at,
                        WorkResult(
                            JobStatus.SKIPPED_ALREADY_RUNNING,
                            "lock",
                            reason_type="ConflictGroupBusy",
                        ),
                        lock_name=lock.name,
                    )
                return _finished(
                    spec,
                    started_at,
                    executor(request),
                    lock_name=lock.name,
                )
        return _finished(spec, started_at, executor(request))
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return _finished(spec, started_at, _failed(spec.name, error))


def render_job_result(result: JobResult) -> str:
    payload: dict[str, Any] = {
        "counts": dict(sorted(result.counts.items())),
        "duration_ms": result.duration_ms,
        "finished_at": result.finished_at.isoformat(),
        "job": result.job_name,
        "stage": result.stage,
        "started_at": result.started_at.isoformat(),
        "status": result.status.value,
    }
    if result.reason_type:
        payload["reason_type"] = result.reason_type
    if result.details:
        payload["details"] = list(result.details)
    if result.lock_name:
        payload["lock"] = result.lock_name
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def job_catalog_payload() -> str:
    jobs = [
        {
            "bounds": list(spec.bounds),
            "cadence": spec.cadence,
            "command": spec.command,
            "conflict_group": spec.conflict_group,
            "description": spec.description,
            "job": spec.name,
            "lock_strategy": spec.lock_strategy,
            "lock_target": spec.lock_target.value if spec.lock_target else None,
            "parallel_safe": spec.parallel_safe,
            "required_features": list(spec.required_features),
            "schedule_class": spec.schedule_class.value,
        }
        for spec in JOB_CATALOG
    ]
    return json.dumps(
        {"jobs": jobs, "operation": "job_catalog", "read_only": True},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
