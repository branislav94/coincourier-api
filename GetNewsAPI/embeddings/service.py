"""Directly invokable Phase 6B1 embedding job engine."""

from __future__ import annotations

import logging
import time
from typing import Callable

from vector_store.models import VectorChunkWrite, VectorDocumentRecord
from vector_store.repository import VectorRepository

from .chunking import prepare_document
from .models import EmbeddingJobResult, EmbeddingSettings
from .provider import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingProviderUnavailable,
    InvalidEmbeddingResponse,
    validate_embedding_batch,
)


logger = logging.getLogger(__name__)


class InvalidEmbeddingInput(ValueError):
    pass


class EmbeddingClaimLostError(RuntimeError):
    pass


ContentLoader = Callable[[VectorDocumentRecord], str]


def safe_embedding_error(error: BaseException, operation: str) -> str:
    error_type = type(error).__name__ or "Error"
    return f"{error_type}: embedding {operation} failed"[:500]


class EmbeddingJobEngine:
    def __init__(
        self,
        *,
        repository: VectorRepository,
        provider: EmbeddingProvider,
        settings: EmbeddingSettings,
        content_loader: ContentLoader,
    ) -> None:
        if provider.provider_name != settings.provider:
            raise EmbeddingConfigurationError("embedding provider identity mismatch")
        if provider.model != settings.model:
            raise EmbeddingConfigurationError("embedding provider model mismatch")
        self.repository = repository
        self.provider = provider
        self.settings = settings
        self.content_loader = content_loader

    def process_next(self, *, claim_timeout_minutes: int = 30) -> EmbeddingJobResult:
        if not self.settings.enabled:
            return EmbeddingJobResult(status="disabled")
        claim = self.repository.claim_embedding_job(
            self.settings.embedding_version,
            timeout_minutes=claim_timeout_minutes,
        )
        if claim is None:
            return EmbeddingJobResult(status="idle")

        provider_calls = 0
        started = time.perf_counter()
        try:
            if claim.embedding_version != self.settings.embedding_version:
                raise EmbeddingConfigurationError("claimed embedding version mismatch")
            body = self.content_loader(claim.document)
            prepared = prepare_document(
                claim.document.title,
                body,
                chunker_version=self.settings.chunker_version,
            )
            if not prepared.chunks:
                raise InvalidEmbeddingInput("document has no embeddable text")
            if prepared.content_hash != claim.document.content_hash:
                raise InvalidEmbeddingInput("document content hash mismatch")
            if prepared.content_version != claim.document.content_version:
                raise InvalidEmbeddingInput("document content version mismatch")

            expected_hashes = tuple(chunk.sha256 for chunk in prepared.chunks)
            reconciliation = self.repository.complete_embedding_job_if_chunks_match(
                claim.id,
                claim.token,
                expected_chunk_hashes=expected_hashes,
                embedding_model=self.settings.model,
                embedding_dimensions=self.settings.dimensions,
            )
            if reconciliation is None:
                raise EmbeddingClaimLostError(
                    "embedding claim ownership was lost before provider work"
                )
            if reconciliation:
                self._log_result(
                    claim_id=claim.id,
                    document_id=claim.document.id,
                    attempt=claim.attempt,
                    token=claim.token,
                    input_count=len(prepared.chunks),
                    usage_tokens=None,
                    elapsed=time.perf_counter() - started,
                    decision="reconciled",
                )
                return EmbeddingJobResult(
                    status="completed",
                    job_id=claim.id,
                    document_id=claim.document.id,
                    attempt=claim.attempt,
                    chunk_count=len(prepared.chunks),
                    provider_calls=0,
                    reconciled=True,
                )

            if len(prepared.chunks) > self.settings.max_chunks_per_job:
                raise InvalidEmbeddingInput("document exceeds embedding chunk limit")

            vectors: list[tuple[float, ...]] = []
            usage_tokens = 0
            for start_index in range(0, len(prepared.chunks), self.settings.batch_size):
                batch_chunks = prepared.chunks[
                    start_index : start_index + self.settings.batch_size
                ]
                provider_calls += 1
                batch = self.provider.embed(
                    [chunk.text for chunk in batch_chunks],
                    self.settings.dimensions,
                )
                vectors.extend(
                    validate_embedding_batch(
                        batch,
                        expected_provider=self.settings.provider,
                        expected_model=self.settings.model,
                        expected_dimensions=self.settings.dimensions,
                        expected_count=len(batch_chunks),
                    )
                )
                if batch.usage_tokens is not None:
                    usage_tokens += batch.usage_tokens

            writes = tuple(
                VectorChunkWrite(
                    chunk_index=chunk.index,
                    chunk_text=chunk.text,
                    chunk_hash=chunk.sha256,
                    embedding=vector,
                )
                for chunk, vector in zip(prepared.chunks, vectors, strict=True)
            )
            if not self.repository.persist_embedding_chunks_and_complete(
                claim.id,
                claim.token,
                chunks=writes,
                embedding_model=self.settings.model,
                embedding_version=self.settings.embedding_version,
            ):
                raise EmbeddingClaimLostError(
                    "embedding claim ownership was lost before completion"
                )

            self._log_result(
                claim_id=claim.id,
                document_id=claim.document.id,
                attempt=claim.attempt,
                token=claim.token,
                input_count=len(prepared.chunks),
                usage_tokens=usage_tokens or None,
                elapsed=time.perf_counter() - started,
                decision="completed",
            )
            return EmbeddingJobResult(
                status="completed",
                job_id=claim.id,
                document_id=claim.document.id,
                attempt=claim.attempt,
                chunk_count=len(prepared.chunks),
                provider_calls=provider_calls,
                embedding_tokens=usage_tokens,
            )
        except (
            EmbeddingConfigurationError,
            InvalidEmbeddingInput,
            InvalidEmbeddingResponse,
        ) as error:
            owned = self.repository.fail_embedding_job(
                claim.id,
                claim.token,
                safe_embedding_error(error, "validation"),
                terminal=True,
            )
            logger.warning(
                "[EMBEDDING] job_id=%s document_id=%s attempt=%s claim=%s "
                "error_type=%s decision=%s",
                claim.id,
                claim.document.id,
                claim.attempt,
                claim.token[:8],
                type(error).__name__,
                "failed" if owned else "lost-claim",
            )
            return EmbeddingJobResult(
                status="failed" if owned else "lost_claim",
                job_id=claim.id,
                document_id=claim.document.id,
                attempt=claim.attempt,
                provider_calls=provider_calls,
            )
        except EmbeddingClaimLostError:
            logger.warning(
                "[EMBEDDING] job_id=%s document_id=%s attempt=%s claim=%s "
                "decision=lost-claim",
                claim.id,
                claim.document.id,
                claim.attempt,
                claim.token[:8],
            )
            return EmbeddingJobResult(
                status="lost_claim",
                job_id=claim.id,
                document_id=claim.document.id,
                attempt=claim.attempt,
                provider_calls=provider_calls,
            )
        except EmbeddingProviderUnavailable as error:
            owned = self.repository.fail_embedding_job(
                claim.id,
                claim.token,
                safe_embedding_error(error, "provider"),
                terminal=False,
            )
            logger.warning(
                "[EMBEDDING] job_id=%s document_id=%s attempt=%s claim=%s "
                "error_type=%s decision=%s",
                claim.id,
                claim.document.id,
                claim.attempt,
                claim.token[:8],
                type(error).__name__,
                "retryable" if owned else "lost-claim",
            )
            return EmbeddingJobResult(
                status="retryable" if owned else "lost_claim",
                job_id=claim.id,
                document_id=claim.document.id,
                attempt=claim.attempt,
                provider_calls=provider_calls,
            )
        except Exception as error:
            try:
                owned = self.repository.fail_embedding_job(
                    claim.id,
                    claim.token,
                    safe_embedding_error(error, "internal"),
                    terminal=False,
                )
            except Exception as cleanup_error:
                logger.error(
                    "[EMBEDDING] job_id=%s document_id=%s attempt=%s claim=%s "
                    "error_type=%s cleanup_error_type=%s "
                    "decision=internal-error-cleanup-failed",
                    claim.id,
                    claim.document.id,
                    claim.attempt,
                    claim.token[:8],
                    type(error).__name__,
                    type(cleanup_error).__name__,
                )
                raise error from cleanup_error
            logger.error(
                "[EMBEDDING] job_id=%s document_id=%s attempt=%s claim=%s "
                "error_type=%s decision=%s",
                claim.id,
                claim.document.id,
                claim.attempt,
                claim.token[:8],
                type(error).__name__,
                "internal-error-released" if owned else "internal-error-lost-claim",
            )
            raise

    def _log_result(
        self,
        *,
        claim_id: int,
        document_id: int,
        attempt: int,
        token: str,
        input_count: int,
        usage_tokens: int | None,
        elapsed: float,
        decision: str,
    ) -> None:
        logger.info(
            "[EMBEDDING] job_id=%s document_id=%s attempt=%s claim=%s "
            "provider=%s model=%s dimensions=%s input_count=%s "
            "usage_tokens=%s elapsed_ms=%s decision=%s",
            claim_id,
            document_id,
            attempt,
            token[:8],
            self.settings.provider,
            self.settings.model,
            self.settings.dimensions,
            input_count,
            usage_tokens,
            round(elapsed * 1000),
            decision,
        )
