"""Bounded source-article semantic retrieval with no decision policy."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Protocol, Sequence

from vector_store.models import (
    EmbeddingJobRecord,
    SourceType,
    VectorChunkRecord,
    VectorDocumentRecord,
    VectorMatch,
)

from .models import (
    CHUNK_OVERSAMPLE_FACTOR,
    MAX_CHUNK_MATCHES_PER_QUERY,
    MAX_QUERY_CHUNKS,
    MAX_SEMANTIC_TOP_K,
    SemanticCandidate,
    SemanticRetrievalResult,
    SemanticRetrievalSettings,
    SemanticRetrievalStatus,
)


class SemanticVectorRepository(Protocol):
    def get_latest_source_document(
        self,
        source_article_id: int,
    ) -> VectorDocumentRecord | None:
        ...

    def get_document_embedding_job(
        self,
        document_id: int,
        embedding_version: str,
    ) -> EmbeddingJobRecord | None:
        ...

    def get_chunks(
        self,
        document_id: int,
        *,
        embedding_version: str | None = None,
    ) -> list[VectorChunkRecord]:
        ...

    def nearest_chunks(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        embedding_version: str,
        source_type: SourceType | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        exclude_document_id: int | None = None,
        exclude_source_article_id: int | None = None,
    ) -> list[VectorMatch]:
        ...


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _select_query_chunks(
    chunks: Sequence[VectorChunkRecord],
) -> tuple[VectorChunkRecord, ...]:
    ordered = sorted(chunks, key=lambda chunk: (chunk.chunk_index, chunk.id))
    if len(ordered) <= MAX_QUERY_CHUNKS:
        return tuple(ordered)
    indexes = [
        position * (len(ordered) - 1) // (MAX_QUERY_CHUNKS - 1)
        for position in range(MAX_QUERY_CHUNKS)
    ]
    return tuple(ordered[index] for index in indexes)


class SemanticRetrievalService:
    def __init__(
        self,
        *,
        repository: SemanticVectorRepository,
        settings: SemanticRetrievalSettings,
    ) -> None:
        self.repository = repository
        self.settings = settings

    def retrieve_source_neighbors(
        self,
        source_article_id: int,
        *,
        top_k: int | None = None,
    ) -> SemanticRetrievalResult:
        if source_article_id <= 0:
            raise ValueError("source_article_id must be positive")
        requested_top_k = self.settings.top_k if top_k is None else int(top_k)
        if not 1 <= requested_top_k <= MAX_SEMANTIC_TOP_K:
            raise ValueError(
                f"semantic top_k must be between 1 and {MAX_SEMANTIC_TOP_K}"
            )
        chunk_limit = min(
            requested_top_k * CHUNK_OVERSAMPLE_FACTOR,
            MAX_CHUNK_MATCHES_PER_QUERY,
        )

        if not self.settings.vector_enabled:
            return self._result(
                SemanticRetrievalStatus.DISABLED,
                source_article_id,
                requested_top_k,
                chunk_limit,
                reason="vector_disabled",
            )
        if not self.settings.semantic_enabled:
            return self._result(
                SemanticRetrievalStatus.DISABLED,
                source_article_id,
                requested_top_k,
                chunk_limit,
                reason="semantic_disabled",
            )

        document = self.repository.get_latest_source_document(source_article_id)
        if document is None:
            return self._result(
                SemanticRetrievalStatus.QUERY_NOT_FOUND,
                source_article_id,
                requested_top_k,
                chunk_limit,
                reason="source_document_not_found",
            )
        if (
            document.source_type is not SourceType.SOURCE_ARTICLE
            or document.source_article_id != source_article_id
            or document.rich_article_id is not None
        ):
            return self._result(
                SemanticRetrievalStatus.QUERY_NOT_READY,
                source_article_id,
                requested_top_k,
                chunk_limit,
                document=document,
                reason="query_document_is_not_source_article",
            )
        if document.published_at is None:
            return self._result(
                SemanticRetrievalStatus.QUERY_NOT_READY,
                source_article_id,
                requested_top_k,
                chunk_limit,
                document=document,
                reason="query_publication_time_missing",
            )

        job = self.repository.get_document_embedding_job(
            document.id,
            self.settings.embedding_version,
        )
        if job is None or job.status != "completed":
            return self._result(
                SemanticRetrievalStatus.QUERY_NOT_READY,
                source_article_id,
                requested_top_k,
                chunk_limit,
                document=document,
                reason="query_embedding_incomplete",
            )
        chunks = [
            chunk
            for chunk in self.repository.get_chunks(
                document.id,
                embedding_version=self.settings.embedding_version,
            )
            if chunk.embedding_version == self.settings.embedding_version
        ]
        if not chunks:
            return self._result(
                SemanticRetrievalStatus.QUERY_NOT_READY,
                source_article_id,
                requested_top_k,
                chunk_limit,
                document=document,
                reason="query_vectors_missing",
            )

        query_time = _utc_naive(document.published_at)
        published_after = query_time - timedelta(hours=self.settings.lookback_hours)
        query_chunks = _select_query_chunks(chunks)
        matches_by_source: dict[int, list[tuple[int, VectorMatch]]] = defaultdict(list)
        for query_chunk in query_chunks:
            matches = self.repository.nearest_chunks(
                query_chunk.embedding,
                top_k=chunk_limit,
                embedding_version=self.settings.embedding_version,
                source_type=SourceType.SOURCE_ARTICLE,
                published_after=published_after,
                published_before=query_time,
                exclude_document_id=document.id,
                exclude_source_article_id=source_article_id,
            )
            for match in matches[:chunk_limit]:
                if not self._eligible_match(
                    match,
                    query_document=document,
                    published_after=published_after,
                    published_before=query_time,
                ):
                    continue
                matches_by_source[match.source_article_id].append(
                    (query_chunk.chunk_index, match)
                )

        candidates = tuple(
            sorted(
                (
                    self._candidate(document, query_time, source_matches)
                    for source_matches in matches_by_source.values()
                ),
                key=lambda item: (
                    item.native_distance,
                    item.candidate_source_article_id,
                    item.candidate_document_id,
                    item.best_query_chunk_index,
                    item.best_candidate_chunk_index,
                ),
            )[:requested_top_k]
        )
        status = (
            SemanticRetrievalStatus.RETRIEVED
            if candidates
            else SemanticRetrievalStatus.NO_CANDIDATES
        )
        return SemanticRetrievalResult(
            status=status,
            reason=None,
            query_source_article_id=source_article_id,
            embedding_version=self.settings.embedding_version,
            requested_top_k=requested_top_k,
            lookback_hours=self.settings.lookback_hours,
            query_document_id=document.id,
            query_published_at=query_time,
            query_chunks_available=len(chunks),
            query_chunks_considered=len(query_chunks),
            chunk_matches_per_query=chunk_limit,
            candidates=candidates,
        )

    def _eligible_match(
        self,
        match: VectorMatch,
        *,
        query_document: VectorDocumentRecord,
        published_after: datetime,
        published_before: datetime,
    ) -> bool:
        if match.source_type is not SourceType.SOURCE_ARTICLE:
            return False
        if match.document_id == query_document.id:
            return False
        if match.source_article_id == query_document.source_article_id:
            return False
        if match.embedding_version != self.settings.embedding_version:
            return False
        if match.published_at is None:
            return False
        candidate_time = _utc_naive(match.published_at)
        if not published_after <= candidate_time <= published_before:
            return False
        if not math.isfinite(match.distance):
            raise RuntimeError("semantic retrieval returned a non-finite distance")
        return True

    def _candidate(
        self,
        query_document: VectorDocumentRecord,
        query_time: datetime,
        source_matches: Sequence[tuple[int, VectorMatch]],
    ) -> SemanticCandidate:
        best_query_chunk_index, best_match = min(
            source_matches,
            key=lambda item: (
                item[1].distance,
                item[0],
                item[1].chunk_index,
                item[1].document_id,
                item[1].chunk_id,
            ),
        )
        candidate_time = _utc_naive(best_match.published_at)  # type: ignore[arg-type]
        matched_query_chunks = {query_index for query_index, _match in source_matches}
        return SemanticCandidate(
            query_document_id=query_document.id,
            query_source_article_id=query_document.source_article_id,
            candidate_document_id=best_match.document_id,
            candidate_source_article_id=best_match.source_article_id,
            candidate_document_key=best_match.document_key,
            candidate_source_url=best_match.source_url,
            candidate_title=best_match.title,
            native_distance=best_match.distance,
            embedding_version=best_match.embedding_version,
            best_query_chunk_index=best_query_chunk_index,
            best_candidate_chunk_index=best_match.chunk_index,
            matched_query_chunk_count=len(matched_query_chunks),
            published_at=candidate_time,
            publication_delta_hours=round(
                (query_time - candidate_time).total_seconds() / 3600,
                6,
            ),
            source_type=best_match.source_type,
        )

    def _result(
        self,
        status: SemanticRetrievalStatus,
        source_article_id: int,
        requested_top_k: int,
        chunk_limit: int,
        *,
        reason: str,
        document: VectorDocumentRecord | None = None,
    ) -> SemanticRetrievalResult:
        return SemanticRetrievalResult(
            status=status,
            reason=reason,
            query_source_article_id=source_article_id,
            embedding_version=self.settings.embedding_version,
            requested_top_k=requested_top_k,
            lookback_hours=self.settings.lookback_hours,
            query_document_id=document.id if document else None,
            query_published_at=(
                _utc_naive(document.published_at)
                if document and document.published_at is not None
                else None
            ),
            chunk_matches_per_query=chunk_limit,
        )
