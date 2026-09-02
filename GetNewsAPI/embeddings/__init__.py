"""Phase 6B1 deterministic chunking and embedding job machinery."""

from .chunking import (
    chunk_text,
    normalize_document_text,
    normalize_text,
    prepare_document,
)
from .models import (
    CHUNKER_VERSION,
    EmbeddingBatch,
    EmbeddingJobResult,
    EmbeddingSettings,
    PreparedDocument,
    TextChunk,
)
from .provider import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingProviderUnavailable,
    FakeEmbeddingProvider,
    InvalidEmbeddingResponse,
    OpenAIEmbeddingProvider,
)
from .service import EmbeddingJobEngine

__all__ = [
    "CHUNKER_VERSION",
    "EmbeddingBatch",
    "EmbeddingConfigurationError",
    "EmbeddingJobEngine",
    "EmbeddingJobResult",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "EmbeddingProviderUnavailable",
    "EmbeddingSettings",
    "FakeEmbeddingProvider",
    "InvalidEmbeddingResponse",
    "OpenAIEmbeddingProvider",
    "PreparedDocument",
    "TextChunk",
    "chunk_text",
    "normalize_document_text",
    "normalize_text",
    "prepare_document",
]
