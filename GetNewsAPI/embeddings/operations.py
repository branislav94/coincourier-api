"""Bounded Phase 6B2 ingestion, worker, and backfill task operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from typing import Any

from repositories.embedding_articles import EmbeddingArticleRepository
from vector_store.models import SourceType
from vector_store.repository import VectorRepository

from .ingestion import EmbeddingIngestionService, RegistrationResult
from .models import EmbeddingSettings
from .provider import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from .service import EmbeddingJobEngine


logger = logging.getLogger(__name__)

APPROVED_PROVIDER = "openai"
APPROVED_MODEL = "text-embedding-3-small"
APPROVED_DIMENSIONS = 1536
APPROVED_CHUNKER_VERSION = "chunk-v1"


@dataclass
class IngestionRunMetrics:
    status: str = "completed"
    documents_scanned: int = 0
    documents_registered: int = 0
    jobs_enqueued: int = 0
    jobs_skipped_existing: int = 0
    documents_skipped: int = 0


@dataclass
class BackfillRunMetrics(IngestionRunMetrics):
    pages_scanned: int = 0
    high_water_id: int = 0
    last_scanned_id: int = 0


@dataclass
class WorkerRunMetrics:
    status: str = "completed"
    jobs_claimed: int = 0
    jobs_completed: int = 0
    jobs_reconciled: int = 0
    jobs_retryable: int = 0
    jobs_failed: int = 0
    jobs_lost_claim: int = 0
    provider_calls: int = 0
    chunks_embedded: int = 0
    embedding_tokens: int = 0
    queue_empty: bool = False


def _positive_limit(value: int, name: str, *, maximum: int = 1000) -> int:
    normalized = int(value)
    if not 1 <= normalized <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return normalized


def _log_metrics(event: str, metrics: Any) -> None:
    values = " ".join(
        f"{field.name}={getattr(metrics, field.name)}" for field in fields(metrics)
    )
    logger.info("[%s] %s", event, values)


def _record_registration(
    metrics: IngestionRunMetrics,
    result: RegistrationResult,
) -> None:
    metrics.documents_scanned += 1
    if result.document_created:
        metrics.documents_registered += 1
    if result.job_created:
        metrics.jobs_enqueued += 1
    elif result.job_id is not None:
        metrics.jobs_skipped_existing += 1
    if result.status == "skipped":
        metrics.documents_skipped += 1


def _validate_approved_settings(settings: EmbeddingSettings) -> None:
    if settings.provider != APPROVED_PROVIDER:
        raise EmbeddingConfigurationError("unsupported embedding provider")
    if settings.model != APPROVED_MODEL:
        raise EmbeddingConfigurationError("unsupported embedding model")
    if settings.dimensions != APPROVED_DIMENSIONS:
        raise EmbeddingConfigurationError("unsupported embedding dimensions")
    if settings.chunker_version != APPROVED_CHUNKER_VERSION:
        raise EmbeddingConfigurationError("unsupported embedding chunker version")


def _validate_vector_db_config(vector_db_config: dict[str, Any]) -> None:
    missing = [
        key
        for key in ("user", "password", "host", "database")
        if not vector_db_config.get(key)
    ]
    if missing:
        raise EmbeddingConfigurationError(
            "vector database configuration is incomplete: " + ",".join(missing)
        )
    try:
        port = int(vector_db_config.get("port", 0))
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigurationError("vector database port is invalid") from exc
    if not 1 <= port <= 65535:
        raise EmbeddingConfigurationError("vector database port is invalid")


def _configured_ingestion_defaults() -> tuple[bool, EmbeddingSettings, int]:
    from config import EMBEDDING_INGEST_LIMIT, VECTOR_ENABLED

    return VECTOR_ENABLED, EmbeddingSettings.from_config(), EMBEDDING_INGEST_LIMIT


def run_embedding_ingest(
    *,
    limit: int | None = None,
    vector_enabled: bool | None = None,
    settings: EmbeddingSettings | None = None,
    vector_repository: VectorRepository | None = None,
    article_repository: EmbeddingArticleRepository | None = None,
    vector_db_config: dict[str, Any] | None = None,
) -> IngestionRunMetrics:
    if vector_enabled is None or settings is None or limit is None:
        configured_enabled, configured_settings, configured_limit = (
            _configured_ingestion_defaults()
        )
        vector_enabled = configured_enabled if vector_enabled is None else vector_enabled
        settings = configured_settings if settings is None else settings
        limit = configured_limit if limit is None else limit
    metrics = IngestionRunMetrics()
    if not vector_enabled:
        metrics.status = "disabled"
        _log_metrics("EMBEDDING-INGEST", metrics)
        return metrics

    assert settings is not None and limit is not None
    _validate_approved_settings(settings)
    limit = _positive_limit(limit, "embedding ingest limit")
    if vector_repository is None:
        if vector_db_config is None:
            from config import VECTOR_DB_CONFIG

            vector_db_config = VECTOR_DB_CONFIG
        _validate_vector_db_config(vector_db_config)
        vector_repository = VectorRepository()
    if article_repository is None:
        article_repository = EmbeddingArticleRepository()

    vector_repository.check_connection()
    article_repository.check_connection()
    service = EmbeddingIngestionService(
        vector_repository=vector_repository,
        article_repository=article_repository,
        settings=settings,
    )
    for article in article_repository.scan_recent(limit):
        _record_registration(metrics, service.register(article))
    _log_metrics("EMBEDDING-INGEST", metrics)
    return metrics


def run_embedding_backfill(
    source_type: SourceType,
    *,
    limit: int | None = None,
    page_size: int | None = None,
    vector_enabled: bool | None = None,
    settings: EmbeddingSettings | None = None,
    vector_repository: VectorRepository | None = None,
    article_repository: EmbeddingArticleRepository | None = None,
    vector_db_config: dict[str, Any] | None = None,
) -> BackfillRunMetrics:
    if vector_enabled is None or settings is None or limit is None or page_size is None:
        from config import (
            EMBEDDING_BACKFILL_PAGE_SIZE,
            EMBEDDING_INGEST_LIMIT,
            VECTOR_ENABLED,
        )

        vector_enabled = VECTOR_ENABLED if vector_enabled is None else vector_enabled
        settings = EmbeddingSettings.from_config() if settings is None else settings
        limit = EMBEDDING_INGEST_LIMIT if limit is None else limit
        page_size = (
            EMBEDDING_BACKFILL_PAGE_SIZE if page_size is None else page_size
        )
    metrics = BackfillRunMetrics()
    if not vector_enabled:
        metrics.status = "disabled"
        _log_metrics("EMBEDDING-BACKFILL", metrics)
        return metrics

    assert settings is not None and limit is not None and page_size is not None
    _validate_approved_settings(settings)
    limit = _positive_limit(limit, "embedding backfill limit")
    page_size = _positive_limit(page_size, "embedding backfill page size")
    if vector_repository is None:
        if vector_db_config is None:
            from config import VECTOR_DB_CONFIG

            vector_db_config = VECTOR_DB_CONFIG
        _validate_vector_db_config(vector_db_config)
        vector_repository = VectorRepository()
    if article_repository is None:
        article_repository = EmbeddingArticleRepository()

    vector_repository.check_connection()
    article_repository.check_connection()
    service = EmbeddingIngestionService(
        vector_repository=vector_repository,
        article_repository=article_repository,
        settings=settings,
    )
    metrics.high_water_id = article_repository.max_article_id(source_type)
    before_id = metrics.high_water_id + 1
    changed = 0
    while changed < limit and before_id > 1:
        page = article_repository.scan_backfill_page(
            source_type,
            before_id=before_id,
            page_size=page_size,
        )
        metrics.pages_scanned += 1
        if not page:
            break
        next_before_id = min(article.cursor_id for article in page)
        if next_before_id <= 0 or next_before_id >= before_id:
            raise RuntimeError("embedding backfill cursor did not advance")
        for article in page:
            result = service.register(article)
            _record_registration(metrics, result)
            metrics.last_scanned_id = article.cursor_id
            if result.changed:
                changed += 1
                if changed >= limit:
                    break
        before_id = next_before_id

    _log_metrics("EMBEDDING-BACKFILL", metrics)
    return metrics


def run_embedding_worker(
    *,
    limit: int | None = None,
    claim_timeout_minutes: int | None = None,
    vector_enabled: bool | None = None,
    settings: EmbeddingSettings | None = None,
    vector_repository: VectorRepository | None = None,
    article_repository: EmbeddingArticleRepository | None = None,
    provider: EmbeddingProvider | None = None,
    vector_db_config: dict[str, Any] | None = None,
    openai_api_key: str | None = None,
) -> WorkerRunMetrics:
    if vector_enabled is None:
        from config import VECTOR_ENABLED

        vector_enabled = VECTOR_ENABLED
    if settings is None:
        from config import EMBEDDING_ENABLED

        if not vector_enabled or not EMBEDDING_ENABLED:
            metrics = WorkerRunMetrics(status="disabled")
            _log_metrics("EMBEDDING-WORKER", metrics)
            return metrics
        settings = EmbeddingSettings.from_config()
    if not vector_enabled or not settings.enabled:
        metrics = WorkerRunMetrics(status="disabled")
        _log_metrics("EMBEDDING-WORKER", metrics)
        return metrics

    if limit is None or claim_timeout_minutes is None:
        from config import EMBEDDING_CLAIM_TIMEOUT_MINUTES, EMBEDDING_WORK_LIMIT

        limit = EMBEDDING_WORK_LIMIT if limit is None else limit
        claim_timeout_minutes = (
            EMBEDDING_CLAIM_TIMEOUT_MINUTES
            if claim_timeout_minutes is None
            else claim_timeout_minutes
        )
    _validate_approved_settings(settings)
    limit = _positive_limit(int(limit), "embedding work limit", maximum=100)
    claim_timeout_minutes = _positive_limit(
        int(claim_timeout_minutes),
        "embedding claim timeout",
        maximum=1440,
    )

    if provider is None:
        if openai_api_key is None:
            from config import OPENAI_API_KEY

            openai_api_key = OPENAI_API_KEY
        if not openai_api_key:
            raise EmbeddingConfigurationError("OpenAI embedding API key is missing")
        provider = OpenAIEmbeddingProvider(
            model=settings.model,
            api_key=openai_api_key,
        )
    if provider.provider_name != settings.provider or provider.model != settings.model:
        raise EmbeddingConfigurationError("embedding provider contract mismatch")

    if vector_repository is None:
        if vector_db_config is None:
            from config import VECTOR_DB_CONFIG

            vector_db_config = VECTOR_DB_CONFIG
        _validate_vector_db_config(vector_db_config)
        vector_repository = VectorRepository()
    if article_repository is None:
        article_repository = EmbeddingArticleRepository()

    vector_repository.check_connection()
    article_repository.check_connection()
    ingestion = EmbeddingIngestionService(
        vector_repository=vector_repository,
        article_repository=article_repository,
        settings=settings,
    )
    engine = EmbeddingJobEngine(
        repository=vector_repository,
        provider=provider,
        settings=settings,
        content_loader=ingestion.load_content,
    )
    metrics = WorkerRunMetrics()
    for _index in range(limit):
        result = engine.process_next(
            claim_timeout_minutes=claim_timeout_minutes,
        )
        if result.status == "idle":
            metrics.queue_empty = True
            break
        if result.job_id is not None:
            metrics.jobs_claimed += 1
        metrics.provider_calls += result.provider_calls
        metrics.embedding_tokens += result.embedding_tokens
        if result.status == "completed":
            metrics.jobs_completed += 1
            if result.reconciled:
                metrics.jobs_reconciled += 1
            else:
                metrics.chunks_embedded += result.chunk_count
        elif result.status == "retryable":
            metrics.jobs_retryable += 1
            break
        elif result.status == "failed":
            metrics.jobs_failed += 1
        elif result.status == "lost_claim":
            metrics.jobs_lost_claim += 1
        else:
            raise RuntimeError(f"unexpected embedding job result: {result.status}")

    _log_metrics("EMBEDDING-WORKER", metrics)
    return metrics
