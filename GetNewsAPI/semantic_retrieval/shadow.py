"""Fail-open Phase 6C2A orchestration for durable semantic evidence."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from vector_store.models import SourceType
from vector_store.repository import VectorRepository
from vector_store.semantic_assessments import (
    SemanticAssessmentDraft,
    SemanticAssessmentRepository,
    SemanticAssessmentStatus,
)

from .models import (
    SemanticCandidate,
    SemanticRetrievalResult,
    SemanticRetrievalSettings,
    SemanticRetrievalStatus,
)
from .service import SemanticRetrievalService, SemanticVectorRepository


logger = logging.getLogger(__name__)
DEFAULT_SEMANTIC_EVIDENCE_VERSION = "semantic-shadow-v1"


class SemanticAssessmentWriter(Protocol):
    def save_assessment(self, draft: SemanticAssessmentDraft) -> int:
        ...


class _TrackingRepository:
    """Remember the query identity already observed by the retrieval service."""

    def __init__(self, repository: SemanticVectorRepository) -> None:
        self._repository = repository
        self.query_document_id: int | None = None

    def get_latest_source_document(self, source_article_id: int):
        document = self._repository.get_latest_source_document(source_article_id)
        self.query_document_id = document.id if document is not None else None
        return document

    def __getattr__(self, name: str):
        return getattr(self._repository, name)


@dataclass(frozen=True)
class SemanticShadowRunResult:
    status: SemanticAssessmentStatus
    reason: str | None
    query_source_article_id: int
    query_document_id: int | None
    embedding_version: str
    semantic_version: str
    retrieval_status: SemanticRetrievalStatus | None = None
    candidate_count: int = 0
    best_native_distance: float | None = None
    persisted: bool = False
    assessment_id: int | None = None
    error_type: str | None = None


def _configured_defaults() -> tuple[bool, bool, str]:
    from config import (
        SEMANTIC_EVIDENCE_VERSION,
        SEMANTIC_SHADOW_ENABLED,
        VECTOR_ENABLED,
    )

    return VECTOR_ENABLED, SEMANTIC_SHADOW_ENABLED, SEMANTIC_EVIDENCE_VERSION


def _safe_error(error: BaseException, operation: str) -> tuple[str, str]:
    error_type = (type(error).__name__ or "Error")[:128]
    return error_type, f"{error_type}: {operation} failed"[:500]


def _utc_text(value: datetime) -> str:
    normalized = (
        value
        if value.tzinfo is None
        else value.astimezone(UTC).replace(tzinfo=None)
    )
    return normalized.isoformat(timespec="seconds") + "Z"


def _candidate_evidence(
    candidate: SemanticCandidate,
    *,
    query_source_article_id: int,
    embedding_version: str,
) -> dict[str, object]:
    if candidate.source_type is not SourceType.SOURCE_ARTICLE:
        raise ValueError("generated documents cannot be semantic duplicate evidence")
    if candidate.query_source_article_id != query_source_article_id:
        raise ValueError("semantic candidate query source identity mismatch")
    if candidate.candidate_source_article_id == query_source_article_id:
        raise ValueError("same-source semantic evidence is not allowed")
    if candidate.embedding_version != embedding_version:
        raise ValueError("semantic candidate embedding version mismatch")
    if not math.isfinite(candidate.native_distance):
        raise ValueError("semantic candidate distance must be finite")
    return {
        "candidate_source_article_id": candidate.candidate_source_article_id,
        "candidate_document_id": candidate.candidate_document_id,
        "candidate_document_key": candidate.candidate_document_key,
        "candidate_source_url": candidate.candidate_source_url,
        "candidate_title": candidate.candidate_title,
        "native_distance": candidate.native_distance,
        "embedding_version": candidate.embedding_version,
        "best_query_chunk_index": candidate.best_query_chunk_index,
        "best_candidate_chunk_index": candidate.best_candidate_chunk_index,
        "matched_query_chunk_count": candidate.matched_query_chunk_count,
        "published_at": _utc_text(candidate.published_at),
        "publication_delta_hours": candidate.publication_delta_hours,
        "source_type": candidate.source_type.value,
    }


def _assessment_from_retrieval(
    result: SemanticRetrievalResult,
    *,
    semantic_version: str,
) -> SemanticAssessmentDraft:
    status_map = {
        SemanticRetrievalStatus.DISABLED: SemanticAssessmentStatus.DISABLED,
        SemanticRetrievalStatus.QUERY_NOT_FOUND: SemanticAssessmentStatus.NOT_READY,
        SemanticRetrievalStatus.QUERY_NOT_READY: SemanticAssessmentStatus.NOT_READY,
        SemanticRetrievalStatus.NO_CANDIDATES: SemanticAssessmentStatus.NO_CANDIDATES,
        SemanticRetrievalStatus.RETRIEVED: SemanticAssessmentStatus.RETRIEVED,
    }
    status = status_map[result.status]
    evidence = tuple(
        _candidate_evidence(
            candidate,
            query_source_article_id=result.query_source_article_id,
            embedding_version=result.embedding_version,
        )
        for candidate in result.candidates
    )
    if len({item["candidate_source_article_id"] for item in evidence}) != len(evidence):
        raise ValueError("semantic candidate evidence contains duplicate source identities")
    if status is SemanticAssessmentStatus.RETRIEVED and not evidence:
        raise ValueError("retrieved semantic result has no candidate evidence")
    if status is not SemanticAssessmentStatus.RETRIEVED and evidence:
        raise ValueError("unavailable semantic result unexpectedly contains candidates")

    return SemanticAssessmentDraft(
        query_source_article_id=result.query_source_article_id,
        query_document_id=result.query_document_id,
        embedding_version=result.embedding_version,
        semantic_version=semantic_version,
        status=status,
        reason=result.reason,
        lookback_hours=result.lookback_hours,
        requested_top_k=result.requested_top_k,
        query_chunks_available=result.query_chunks_available,
        query_chunks_considered=result.query_chunks_considered,
        best_native_distance=(
            min(candidate.native_distance for candidate in result.candidates)
            if evidence
            else None
        ),
        evidence=evidence,
    )


def _error_assessment(
    *,
    source_article_id: int,
    query_document_id: int | None,
    settings: SemanticRetrievalSettings,
    semantic_version: str,
    error: BaseException,
    operation: str,
) -> SemanticAssessmentDraft:
    error_type, safe_error = _safe_error(error, operation)
    return SemanticAssessmentDraft(
        query_source_article_id=source_article_id,
        query_document_id=query_document_id,
        embedding_version=settings.embedding_version,
        semantic_version=semantic_version,
        status=SemanticAssessmentStatus.ERROR,
        reason=f"{operation}_failed",
        lookback_hours=settings.lookback_hours,
        requested_top_k=settings.top_k,
        error_type=error_type,
        safe_error=safe_error,
    )


def _log_result(result: SemanticShadowRunResult) -> None:
    logger.info(
        "[SEMANTIC-SHADOW] semantic_shadow_%s=true article_id=%s "
        "query_document_id=%s semantic_version=%s semantic_candidate_count=%s "
        "semantic_best_native_distance=%s reason=%s error_type=%s persisted=%s "
        "decision=continue",
        result.status.value,
        result.query_source_article_id,
        result.query_document_id,
        result.semantic_version,
        result.candidate_count,
        result.best_native_distance,
        result.reason,
        result.error_type,
        result.persisted,
    )


def run_semantic_shadow(
    source_article_id: int,
    *,
    vector_enabled: bool | None = None,
    semantic_enabled: bool | None = None,
    semantic_version: str | None = None,
    settings: SemanticRetrievalSettings | None = None,
    vector_repository: SemanticVectorRepository | None = None,
    assessment_repository: SemanticAssessmentWriter | None = None,
) -> SemanticShadowRunResult:
    """Retrieve and persist semantic evidence while converting failures to audit errors."""
    if source_article_id <= 0:
        raise ValueError("source_article_id must be positive")

    configured_vector, configured_semantic, configured_version = _configured_defaults()
    vector_enabled = configured_vector if vector_enabled is None else vector_enabled
    semantic_enabled = configured_semantic if semantic_enabled is None else semantic_enabled
    semantic_version = semantic_version or configured_version

    disabled_reason = None
    if not vector_enabled:
        disabled_reason = "vector_disabled"
    elif not semantic_enabled:
        disabled_reason = "semantic_disabled"
    if disabled_reason is not None:
        result = SemanticShadowRunResult(
            status=SemanticAssessmentStatus.DISABLED,
            reason=disabled_reason,
            query_source_article_id=source_article_id,
            query_document_id=None,
            embedding_version=(settings.embedding_version if settings else "unavailable"),
            semantic_version=semantic_version,
        )
        _log_result(result)
        return result

    repository = vector_repository or VectorRepository()
    tracking_repository = _TrackingRepository(repository)
    writer = assessment_repository or SemanticAssessmentRepository()
    retrieval: SemanticRetrievalResult | None = None
    query_document_id: int | None = None
    try:
        if settings is None:
            settings = SemanticRetrievalSettings.from_config()
            settings = replace(
                settings,
                vector_enabled=True,
                semantic_enabled=True,
            )
        if not semantic_version.strip():
            raise ValueError("semantic evidence version is required")

        logger.info(
            "[SEMANTIC-SHADOW] semantic_shadow_attempted=true article_id=%s "
            "semantic_version=%s",
            source_article_id,
            semantic_version,
        )
        retrieval = SemanticRetrievalService(
            repository=tracking_repository,
            settings=settings,
        ).retrieve_source_neighbors(source_article_id)
        query_document_id = retrieval.query_document_id
        draft = _assessment_from_retrieval(
            retrieval,
            semantic_version=semantic_version,
        )
    except Exception as error:
        if settings is None:
            error_type, _ = _safe_error(error, "configuration")
            result = SemanticShadowRunResult(
                status=SemanticAssessmentStatus.ERROR,
                reason="configuration_failed",
                query_source_article_id=source_article_id,
                query_document_id=None,
                embedding_version="unavailable",
                semantic_version=semantic_version,
                error_type=error_type,
            )
            logger.error(
                "[SEMANTIC-SHADOW] semantic_shadow_error=true article_id=%s "
                "error_type=%s reason=configuration_failed persisted=false decision=continue",
                source_article_id,
                error_type,
            )
            return result

        query_document_id = query_document_id or tracking_repository.query_document_id
        draft = _error_assessment(
            source_article_id=source_article_id,
            query_document_id=query_document_id,
            settings=settings,
            semantic_version=semantic_version,
            error=error,
            operation="retrieval",
        )
        retrieval_error = error
    else:
        retrieval_error = None

    try:
        assessment_id = writer.save_assessment(draft)
    except Exception as persistence_error:
        error_type, _ = _safe_error(persistence_error, "persistence")
        result = SemanticShadowRunResult(
            status=SemanticAssessmentStatus.ERROR,
            reason="persistence_failed",
            query_source_article_id=source_article_id,
            query_document_id=query_document_id,
            embedding_version=draft.embedding_version,
            semantic_version=semantic_version,
            retrieval_status=retrieval.status if retrieval else None,
            candidate_count=len(draft.evidence),
            best_native_distance=draft.best_native_distance,
            error_type=error_type,
        )
        logger.error(
            "[SEMANTIC-SHADOW] semantic_shadow_error=true article_id=%s "
            "error_type=%s reason=persistence_failed persisted=false decision=continue",
            source_article_id,
            error_type,
        )
        return result

    result = SemanticShadowRunResult(
        status=draft.status,
        reason=draft.reason,
        query_source_article_id=source_article_id,
        query_document_id=query_document_id,
        embedding_version=draft.embedding_version,
        semantic_version=semantic_version,
        retrieval_status=retrieval.status if retrieval else None,
        candidate_count=len(draft.evidence),
        best_native_distance=draft.best_native_distance,
        persisted=True,
        assessment_id=assessment_id,
        error_type=(type(retrieval_error).__name__ if retrieval_error else None),
    )
    _log_result(result)
    return result
