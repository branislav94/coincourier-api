"""Optional MariaDB vector-storage boundary."""

from .models import (
    VECTOR_DIMENSIONS,
    SourceType,
    VectorChunkRecord,
    VectorDocumentDraft,
    VectorDocumentRecord,
    VectorMatch,
)
from .repository import VectorRepository

__all__ = [
    "VECTOR_DIMENSIONS",
    "SourceType",
    "VectorChunkRecord",
    "VectorDocumentDraft",
    "VectorDocumentRecord",
    "VectorMatch",
    "VectorRepository",
]
