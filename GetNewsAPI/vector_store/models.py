"""Typed identities and retrieval results for the separate vector database."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


VECTOR_DIMENSIONS = 1536
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceType(str, Enum):
    SOURCE_ARTICLE = "source_article"
    COINCOURIER_GENERATED = "coincourier_generated"


@dataclass(frozen=True)
class VectorDocumentDraft:
    source_type: SourceType
    source_article_id: int
    rich_article_id: int | None
    source_url: str | None
    title: str
    published_at: datetime | None
    content_hash: str
    content_version: str

    def __post_init__(self) -> None:
        if self.source_article_id <= 0:
            raise ValueError("source_article_id must be positive")
        if self.source_type is SourceType.SOURCE_ARTICLE and self.rich_article_id is not None:
            raise ValueError("source articles cannot carry a rich_article_id")
        if self.source_type is SourceType.COINCOURIER_GENERATED:
            if self.rich_article_id is None or self.rich_article_id <= 0:
                raise ValueError(
                    "CoinCourier-generated documents require both source and rich IDs"
                )
        if not self.title.strip():
            raise ValueError("title is required")
        if not _SHA256_RE.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        if not self.content_version.strip():
            raise ValueError("content_version is required")

    @property
    def document_key(self) -> str:
        if self.source_type is SourceType.SOURCE_ARTICLE:
            return f"source_article:{self.source_article_id}"
        return f"coincourier_generated:{self.rich_article_id}"


@dataclass(frozen=True)
class VectorDocumentRecord:
    id: int
    document_key: str
    source_type: SourceType
    source_article_id: int
    rich_article_id: int | None
    source_url: str | None
    title: str
    published_at: datetime | None
    content_hash: str
    content_version: str


@dataclass(frozen=True)
class VectorChunkRecord:
    id: int
    document_id: int
    chunk_index: int
    chunk_text: str
    chunk_hash: str
    embedding: tuple[float, ...]
    embedding_model: str
    embedding_dimensions: int
    embedding_version: str


@dataclass(frozen=True)
class VectorMatch:
    distance: float
    document_id: int
    document_key: str
    source_type: SourceType
    source_article_id: int
    rich_article_id: int | None
    source_url: str | None
    title: str
    published_at: datetime | None
    chunk_id: int
    chunk_index: int
    chunk_text: str
    chunk_hash: str
    embedding_model: str
    embedding_version: str


@dataclass(frozen=True)
class VectorChunkWrite:
    chunk_index: int
    chunk_text: str
    chunk_hash: str
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class EmbeddingJobRecord:
    id: int
    document_id: int
    embedding_version: str
    status: str
    attempt_count: int
    claim_token: str | None
    claimed_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class EmbeddingJobClaim:
    id: int
    token: str
    document: VectorDocumentRecord
    embedding_version: str
    attempt: int
    recovered: bool = False
