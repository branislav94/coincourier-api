"""Typed Phase 6B1 chunking, provider, and job-engine contracts."""

from __future__ import annotations

from dataclasses import dataclass

from vector_store.models import VECTOR_DIMENSIONS


CHUNKER_VERSION = "chunk-v1"
DEFAULT_EMBEDDING_BATCH_SIZE = 16
DEFAULT_EMBEDDING_MAX_CHUNKS_PER_JOB = 100


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    sha256: str
    token_count: int


@dataclass(frozen=True)
class PreparedDocument:
    text: str
    content_hash: str
    content_version: str
    chunker_version: str
    chunks: tuple[TextChunk, ...]


@dataclass(frozen=True)
class EmbeddingBatch:
    provider: str
    model: str
    dimensions: int
    vectors: tuple[tuple[float, ...], ...]
    usage_tokens: int | None = None


@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool
    provider: str
    model: str
    dimensions: int = VECTOR_DIMENSIONS
    chunker_version: str = CHUNKER_VERSION
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    max_chunks_per_job: int = DEFAULT_EMBEDDING_MAX_CHUNKS_PER_JOB

    def __post_init__(self) -> None:
        if not self.provider or ":" in self.provider:
            raise ValueError("embedding provider must be nonempty and contain no colon")
        if not self.model or ":" in self.model:
            raise ValueError("embedding model must be nonempty and contain no colon")
        if self.dimensions != VECTOR_DIMENSIONS:
            raise ValueError(
                f"Phase 6B1 requires exactly {VECTOR_DIMENSIONS} embedding dimensions"
            )
        if self.chunker_version != CHUNKER_VERSION:
            raise ValueError(f"unsupported chunker version: {self.chunker_version}")
        if not 1 <= self.batch_size <= 100:
            raise ValueError("embedding batch size must be between 1 and 100")
        if not 1 <= self.max_chunks_per_job <= 1000:
            raise ValueError("embedding max chunks per job must be between 1 and 1000")

    @property
    def embedding_version(self) -> str:
        return (
            f"{self.provider}:{self.model}:{self.dimensions}:"
            f"{self.chunker_version}"
        )

    @classmethod
    def from_config(cls) -> "EmbeddingSettings":
        from config import (
            EMBEDDING_BATCH_SIZE,
            EMBEDDING_CHUNKER_VERSION,
            EMBEDDING_DIMENSIONS,
            EMBEDDING_ENABLED,
            EMBEDDING_MAX_CHUNKS_PER_JOB,
            EMBEDDING_MODEL,
            EMBEDDING_PROVIDER,
        )

        return cls(
            enabled=EMBEDDING_ENABLED,
            provider=EMBEDDING_PROVIDER,
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIMENSIONS,
            chunker_version=EMBEDDING_CHUNKER_VERSION,
            batch_size=EMBEDDING_BATCH_SIZE,
            max_chunks_per_job=EMBEDDING_MAX_CHUNKS_PER_JOB,
        )


@dataclass(frozen=True)
class EmbeddingJobResult:
    status: str
    job_id: int | None = None
    document_id: int | None = None
    attempt: int | None = None
    chunk_count: int = 0
    provider_calls: int = 0
    reconciled: bool = False
    embedding_tokens: int = 0
