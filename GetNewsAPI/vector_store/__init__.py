"""Optional MariaDB vector-storage boundary."""

from .models import (
    EmbeddingJobClaim,
    EmbeddingJobRecord,
    VECTOR_DIMENSIONS,
    SourceType,
    VectorChunkRecord,
    VectorDocumentDraft,
    VectorDocumentRecord,
    VectorMatch,
    VectorChunkWrite,
)
from .repository import VectorRepository

__all__ = [
    "EmbeddingJobClaim",
    "EmbeddingJobRecord",
    "VECTOR_DIMENSIONS",
    "SourceType",
    "VectorChunkRecord",
    "VectorDocumentDraft",
    "VectorDocumentRecord",
    "VectorMatch",
    "VectorChunkWrite",
    "VectorRepository",
]
