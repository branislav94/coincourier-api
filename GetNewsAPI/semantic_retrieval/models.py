"""Typed, decision-free contracts for Phase 6C1 semantic retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from vector_store.models import SourceType


DEFAULT_SEMANTIC_LOOKBACK_HOURS = 72
DEFAULT_SEMANTIC_TOP_K = 10
MAX_SEMANTIC_TOP_K = 20
MAX_QUERY_CHUNKS = 8
CHUNK_OVERSAMPLE_FACTOR = 5
MAX_CHUNK_MATCHES_PER_QUERY = 100


class SemanticRetrievalStatus(str, Enum):
    RETRIEVED = "retrieved"
    DISABLED = "disabled"
    QUERY_NOT_FOUND = "query_not_found"
    QUERY_NOT_READY = "query_not_ready"
    NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True)
class SemanticRetrievalSettings:
    vector_enabled: bool
    semantic_enabled: bool
    embedding_version: str
    lookback_hours: int = DEFAULT_SEMANTIC_LOOKBACK_HOURS
    top_k: int = DEFAULT_SEMANTIC_TOP_K

    def __post_init__(self) -> None:
        if not self.embedding_version.strip():
            raise ValueError("semantic embedding_version is required")
        if not 1 <= self.lookback_hours <= 24 * 365:
            raise ValueError("semantic lookback hours must be between 1 and 8760")
        if not 1 <= self.top_k <= MAX_SEMANTIC_TOP_K:
            raise ValueError(
                f"semantic top_k must be between 1 and {MAX_SEMANTIC_TOP_K}"
            )

    @classmethod
    def from_config(cls) -> "SemanticRetrievalSettings":
        from config import (
            SEMANTIC_LOOKBACK_HOURS,
            SEMANTIC_SHADOW_ENABLED,
            SEMANTIC_TOP_K,
            VECTOR_ENABLED,
        )
        from embeddings.models import EmbeddingSettings

        return cls(
            vector_enabled=VECTOR_ENABLED,
            semantic_enabled=SEMANTIC_SHADOW_ENABLED,
            embedding_version=EmbeddingSettings.from_config().embedding_version,
            lookback_hours=SEMANTIC_LOOKBACK_HOURS,
            top_k=SEMANTIC_TOP_K,
        )


@dataclass(frozen=True)
class SemanticCandidate:
    query_document_id: int
    query_source_article_id: int
    candidate_document_id: int
    candidate_source_article_id: int
    candidate_document_key: str
    candidate_source_url: str | None
    candidate_title: str
    native_distance: float
    embedding_version: str
    best_query_chunk_index: int
    best_candidate_chunk_index: int
    matched_query_chunk_count: int
    published_at: datetime
    publication_delta_hours: float
    source_type: SourceType


@dataclass(frozen=True)
class SemanticRetrievalResult:
    status: SemanticRetrievalStatus
    reason: str | None
    query_source_article_id: int
    embedding_version: str
    requested_top_k: int
    lookback_hours: int
    query_document_id: int | None = None
    query_published_at: datetime | None = None
    query_chunks_available: int = 0
    query_chunks_considered: int = 0
    chunk_matches_per_query: int = 0
    candidates: tuple[SemanticCandidate, ...] = ()
