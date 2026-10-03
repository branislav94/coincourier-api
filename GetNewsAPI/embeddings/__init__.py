"""Disabled-by-default Phase 6B embedding subsystem."""

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
from .ingestion import (
    ApplicationArticle,
    EmbeddingIngestionService,
    RegistrationResult,
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
    "ApplicationArticle",
    "EmbeddingBatch",
    "EmbeddingConfigurationError",
    "EmbeddingJobEngine",
    "EmbeddingJobResult",
    "EmbeddingIngestionService",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "EmbeddingProviderUnavailable",
    "EmbeddingSettings",
    "FakeEmbeddingProvider",
    "InvalidEmbeddingResponse",
    "OpenAIEmbeddingProvider",
    "PreparedDocument",
    "RegistrationResult",
    "TextChunk",
    "chunk_text",
    "normalize_document_text",
    "normalize_text",
    "prepare_document",
]
