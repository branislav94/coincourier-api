from __future__ import annotations

import inspect
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, BadRequestError


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config
from embeddings.chunking import (
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_TOKENS,
    chunk_text,
    normalize_document_text,
    prepare_document,
)
from embeddings.models import EmbeddingBatch, EmbeddingSettings
from embeddings.provider import (
    EmbeddingConfigurationError,
    EmbeddingProviderUnavailable,
    FakeEmbeddingProvider,
    InvalidEmbeddingResponse,
    OpenAIEmbeddingProvider,
    validate_embedding_batch,
)
from embeddings.service import EmbeddingJobEngine, safe_embedding_error
from vector_store.models import (
    EmbeddingJobClaim,
    SourceType,
    VECTOR_DIMENSIONS,
    VectorDocumentRecord,
)


SETTINGS = EmbeddingSettings(
    enabled=True,
    provider="openai",
    model="text-embedding-3-small",
    dimensions=VECTOR_DIMENSIONS,
    chunker_version="chunk-v1",
    batch_size=16,
)


class ChunkingTests(unittest.TestCase):
    def test_same_input_produces_identical_chunks_and_order(self):
        body = "First event sentence. Second event sentence.\n\nThird paragraph."
        first = prepare_document("Market update", body)
        second = prepare_document("Market update", body)
        self.assertEqual(first, second)
        self.assertEqual([chunk.index for chunk in first.chunks], list(range(len(first.chunks))))

    def test_document_and_chunk_hashes_are_stable_sha256(self):
        prepared = prepare_document("Bitcoin", "BTC rose 12.5% on 2026-09-01.")
        self.assertEqual(len(prepared.content_hash), 64)
        self.assertEqual(len(prepared.chunks[0].sha256), 64)
        self.assertEqual(
            prepared,
            prepare_document("Bitcoin", "BTC rose 12.5% on 2026-09-01."),
        )

    def test_paragraph_boundaries_are_preserved_when_packed(self):
        normalized = normalize_document_text(
            "Title",
            "First paragraph sentence.\n\nSecond paragraph sentence.",
        )
        self.assertIn("\n\nFirst paragraph sentence.\n\nSecond paragraph", normalized)
        self.assertIn("\n\n", chunk_text(normalized)[0].text)

    def test_equivalent_whitespace_and_html_normalize_identically(self):
        first = prepare_document(
            "  Bitcoin   update ",
            "<p>BTC\trose.</p>\r\n<p>Markets active.</p>",
        )
        second = prepare_document(
            "Bitcoin update",
            "BTC rose.\n\nMarkets active.",
        )
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.chunks, second.chunks)

    def test_numbers_dates_tickers_amounts_and_percentages_remain(self):
        text = normalize_document_text(
            "BTC filing",
            "On 2026-09-01, ACME held 5,000 BTC worth $1.2 billion, up 12.5%.",
        )
        for value in ("2026-09-01", "ACME", "5,000", "BTC", "$1.2", "12.5%"):
            self.assertIn(value, text)

    def test_oversized_text_splits_without_breaking_words(self):
        words = [f"token{index}" for index in range(1400)]
        chunks = chunk_text(" ".join(words))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.token_count <= CHUNK_MAX_TOKENS for chunk in chunks))
        reconstructed = " ".join(chunk.text for chunk in chunks).split()
        self.assertEqual(reconstructed, words)

    def test_chunk_v1_has_no_overlap_or_duplicate_tokens(self):
        words = [f"unique{index}" for index in range(1200)]
        chunks = chunk_text(" ".join(words))
        self.assertEqual(CHUNK_OVERLAP_TOKENS, 0)
        flattened = " ".join(chunk.text for chunk in chunks).split()
        self.assertEqual(flattened, words)

    def test_empty_text_has_no_chunks(self):
        prepared = prepare_document("", " \r\n ")
        self.assertEqual(prepared.text, "")
        self.assertEqual(prepared.chunks, ())

    def test_chunker_version_participates_in_content_identity(self):
        prepared = prepare_document("Title", "Body")
        self.assertEqual(prepared.content_version, f"chunk-v1:{prepared.content_hash}")
        with self.assertRaises(ValueError):
            prepare_document("Title", "Body", chunker_version="chunk-v2")

    def test_changed_content_creates_new_content_identity(self):
        first = prepare_document("Title", "Initial facts.")
        second = prepare_document("Title", "Initial facts plus a material update.")
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.content_version, second.content_version)


class ProviderTests(unittest.TestCase):
    def test_fake_provider_is_deterministic_and_1536_dimensions(self):
        provider = FakeEmbeddingProvider()
        first = provider.embed(["same text"], VECTOR_DIMENSIONS)
        second = provider.embed(["same text"], VECTOR_DIMENSIONS)
        self.assertEqual(first.vectors, second.vectors)
        self.assertEqual(len(first.vectors[0]), VECTOR_DIMENSIONS)
        self.assertTrue(all(math.isfinite(value) for value in first.vectors[0]))

    def test_openai_adapter_sends_explicit_model_input_and_dimensions(self):
        calls = []

        class Embeddings:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    model="text-embedding-3-small",
                    data=[
                        SimpleNamespace(index=1, embedding=[2.0] * VECTOR_DIMENSIONS),
                        SimpleNamespace(index=0, embedding=[1.0] * VECTOR_DIMENSIONS),
                    ],
                    usage=SimpleNamespace(total_tokens=9),
                )

        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            client=SimpleNamespace(embeddings=Embeddings()),
        )
        result = provider.embed(["first", "second"], VECTOR_DIMENSIONS)
        self.assertEqual(
            calls,
            [
                {
                    "model": "text-embedding-3-small",
                    "input": ["first", "second"],
                    "dimensions": VECTOR_DIMENSIONS,
                }
            ],
        )
        self.assertEqual(result.vectors[0][0], 1.0)
        self.assertEqual(result.vectors[1][0], 2.0)
        self.assertEqual(result.usage_tokens, 9)

    def test_openai_adapter_rejects_wrong_response_count(self):
        response = SimpleNamespace(model="text-embedding-3-small", data=[], usage=None)
        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            client=SimpleNamespace(
                embeddings=SimpleNamespace(create=lambda **_kwargs: response)
            ),
        )
        with self.assertRaises(InvalidEmbeddingResponse):
            provider.embed(["expected"], VECTOR_DIMENSIONS)

    def test_openai_adapter_rejects_invalid_response_indexes(self):
        response = SimpleNamespace(
            model="text-embedding-3-small",
            data=[SimpleNamespace(index=None, embedding=[0.0] * VECTOR_DIMENSIONS)],
            usage=None,
        )
        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            client=SimpleNamespace(
                embeddings=SimpleNamespace(create=lambda **_kwargs: response)
            ),
        )
        with self.assertRaises(InvalidEmbeddingResponse):
            provider.embed(["expected"], VECTOR_DIMENSIONS)

    def test_openai_adapter_classifies_transient_client_errors_without_raw_text(self):
        marker = "PRIVATE-ARTICLE-CONTENT"

        def fail(**_kwargs):
            raise APIConnectionError(
                message=marker,
                request=httpx.Request("POST", "https://api.openai.test/embeddings"),
            )

        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            client=SimpleNamespace(embeddings=SimpleNamespace(create=fail)),
        )
        with self.assertRaises(EmbeddingProviderUnavailable) as captured:
            provider.embed([marker], VECTOR_DIMENSIONS)
        self.assertNotIn(marker, str(captured.exception))

    def test_openai_adapter_classifies_rejected_request_as_terminal(self):
        response = httpx.Response(
            400,
            request=httpx.Request("POST", "https://api.openai.test/embeddings"),
        )

        def fail(**_kwargs):
            raise BadRequestError("bad dimensions", response=response, body=None)

        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            client=SimpleNamespace(embeddings=SimpleNamespace(create=fail)),
        )
        with self.assertRaises(EmbeddingConfigurationError):
            provider.embed(["input"], VECTOR_DIMENSIONS)

    def test_openai_adapter_does_not_mask_unexpected_client_defects(self):
        def fail(**_kwargs):
            raise RuntimeError("synthetic programming defect")

        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            client=SimpleNamespace(embeddings=SimpleNamespace(create=fail)),
        )
        with self.assertRaisesRegex(RuntimeError, "synthetic programming defect"):
            provider.embed(["input"], VECTOR_DIMENSIONS)

    def test_batch_validation_rejects_provider_model_and_count_mismatch(self):
        vector = (0.0,) * VECTOR_DIMENSIONS
        for batch in (
            EmbeddingBatch("wrong", SETTINGS.model, VECTOR_DIMENSIONS, (vector,)),
            EmbeddingBatch("openai", "wrong", VECTOR_DIMENSIONS, (vector,)),
            EmbeddingBatch("openai", SETTINGS.model, VECTOR_DIMENSIONS, ()),
        ):
            with self.assertRaises(InvalidEmbeddingResponse):
                validate_embedding_batch(
                    batch,
                    expected_provider="openai",
                    expected_model=SETTINGS.model,
                    expected_dimensions=VECTOR_DIMENSIONS,
                    expected_count=1,
                )

    def test_batch_validation_rejects_wrong_dimensions_nan_and_inf(self):
        vectors = (
            (0.0,) * (VECTOR_DIMENSIONS - 1),
            (0.0,) * (VECTOR_DIMENSIONS - 1) + (math.nan,),
            (0.0,) * (VECTOR_DIMENSIONS - 1) + (math.inf,),
        )
        for vector in vectors:
            batch = EmbeddingBatch(
                "openai",
                SETTINGS.model,
                VECTOR_DIMENSIONS,
                (vector,),
            )
            with self.assertRaises(InvalidEmbeddingResponse):
                validate_embedding_batch(
                    batch,
                    expected_provider="openai",
                    expected_model=SETTINGS.model,
                    expected_dimensions=VECTOR_DIMENSIONS,
                    expected_count=1,
                )

    def test_embedding_version_is_deterministic_and_dimensions_are_fixed(self):
        self.assertEqual(
            SETTINGS.embedding_version,
            "openai:text-embedding-3-small:1536:chunk-v1",
        )
        with self.assertRaises(ValueError):
            EmbeddingSettings(
                enabled=True,
                provider="openai",
                model="text-embedding-3-small",
                dimensions=3072,
            )

    def test_provider_identity_cannot_masquerade_under_embedding_version(self):
        body = "Body facts."
        document = _document("Bitcoin event", body)
        repository = MemoryJobRepository(document, body)
        with self.assertRaises(EmbeddingConfigurationError):
            EmbeddingJobEngine(
                repository=repository,
                provider=FakeEmbeddingProvider(model="different-model"),
                settings=SETTINGS,
                content_loader=lambda _document: body,
            )

    def test_source_defaults_disable_paid_embedding_behavior(self):
        source = inspect.getsource(config)
        self.assertIn('_env_bool("EMBEDDING_ENABLED", False)', source)
        self.assertIn('_env_bool("VECTOR_ENABLED", False)', source)
        env_example = (REPOSITORY_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertIn("EMBEDDING_ENABLED=false", env_example)
        self.assertIn("EMBEDDING_DIMENSIONS=1536", env_example)


def _document(title: str, body: str, *, document_id: int = 1) -> VectorDocumentRecord:
    prepared = prepare_document(title, body)
    return VectorDocumentRecord(
        id=document_id,
        document_key=f"source_article:{document_id}",
        source_type=SourceType.SOURCE_ARTICLE,
        source_article_id=document_id,
        rich_article_id=None,
        source_url=f"https://source.example.test/{document_id}",
        title=title,
        published_at=None,
        content_hash=prepared.content_hash,
        content_version=prepared.content_version,
    )


class MemoryJobRepository:
    def __init__(self, document: VectorDocumentRecord, body: str) -> None:
        self.document = document
        self.body = body
        self.status = "pending"
        self.transaction_open = False
        self.claimed = False
        self.persisted = ()
        self.expected_existing_hashes: tuple[str, ...] = ()
        self.ownership_lost = False
        self.events: list[str] = []
        self.last_error = None

    def claim_embedding_job(self, embedding_version, *, timeout_minutes):
        del timeout_minutes
        self.events.append("claim")
        if self.status == "completed" or self.claimed:
            return None
        self.transaction_open = True
        self.status = "claimed"
        self.claimed = True
        self.transaction_open = False
        return EmbeddingJobClaim(
            id=11,
            token="a" * 64,
            document=self.document,
            embedding_version=embedding_version,
            attempt=1,
        )

    def complete_embedding_job_if_chunks_match(
        self,
        job_id,
        token,
        *,
        expected_chunk_hashes,
        embedding_model,
        embedding_dimensions,
    ):
        del job_id, token, embedding_model, embedding_dimensions
        self.events.append("reconcile")
        if self.ownership_lost:
            return None
        if tuple(expected_chunk_hashes) == self.expected_existing_hashes:
            self.status = "completed"
            return True
        return False

    def persist_embedding_chunks_and_complete(
        self,
        job_id,
        token,
        *,
        chunks,
        embedding_model,
        embedding_version,
    ):
        del job_id, token, embedding_model, embedding_version
        self.events.append("persist")
        self.persisted = tuple(chunks)
        self.status = "completed"
        return True

    def fail_embedding_job(self, job_id, token, safe_error, *, terminal):
        del job_id, token
        self.events.append("fail")
        self.status = "failed" if terminal else "retryable"
        self.last_error = safe_error
        return True


class JobEngineTests(unittest.TestCase):
    def engine(self, body, *, repository=None, provider=None, settings=SETTINGS):
        document = _document("Bitcoin event", body)
        repository = repository or MemoryJobRepository(document, body)
        provider = provider or FakeEmbeddingProvider()
        return (
            EmbeddingJobEngine(
                repository=repository,
                provider=provider,
                settings=settings,
                content_loader=lambda _document: body,
            ),
            repository,
            provider,
        )

    def test_disabled_engine_never_claims_or_calls_provider(self):
        disabled = EmbeddingSettings(
            enabled=False,
            provider="openai",
            model="text-embedding-3-small",
        )
        engine, repository, provider = self.engine("Body", settings=disabled)
        result = engine.process_next()
        self.assertEqual(result.status, "disabled")
        self.assertEqual(repository.events, [])
        self.assertEqual(provider.call_count, 0)

    def test_provider_runs_after_claim_transaction_is_closed(self):
        body = "BTC rose after the filing."
        document = _document("Bitcoin event", body)
        repository = MemoryJobRepository(document, body)

        class BoundaryProvider(FakeEmbeddingProvider):
            def embed(self, texts, dimensions):
                self.test_case.assertFalse(repository.transaction_open)
                repository.events.append("provider")
                return super().embed(texts, dimensions)

        provider = BoundaryProvider()
        provider.test_case = self
        engine, _, _ = self.engine(body, repository=repository, provider=provider)
        result = engine.process_next()
        self.assertEqual(result.status, "completed")
        self.assertEqual(repository.events, ["claim", "reconcile", "provider", "persist"])

    def test_success_persists_vectors_and_completes(self):
        engine, repository, provider = self.engine("BTC rose 12% after approval.")
        result = engine.process_next()
        self.assertEqual(result.status, "completed")
        self.assertEqual(repository.status, "completed")
        self.assertEqual(len(repository.persisted), result.chunk_count)
        self.assertEqual(provider.call_count, 1)

    def test_bounded_batches_preserve_chunk_vector_mapping(self):
        body = " ".join(f"token{index}" for index in range(1300))
        settings = EmbeddingSettings(
            enabled=True,
            provider="openai",
            model="text-embedding-3-small",
            batch_size=1,
        )
        engine, repository, provider = self.engine(body, settings=settings)
        result = engine.process_next()
        self.assertGreater(result.chunk_count, 1)
        self.assertEqual(provider.input_counts, [1] * result.chunk_count)
        self.assertEqual(
            [chunk.chunk_index for chunk in repository.persisted],
            list(range(result.chunk_count)),
        )

    def test_oversized_job_is_terminal_before_provider_work(self):
        body = " ".join(f"token{index}" for index in range(1300))
        settings = EmbeddingSettings(
            enabled=True,
            provider="openai",
            model="text-embedding-3-small",
            max_chunks_per_job=1,
        )
        engine, repository, provider = self.engine(body, settings=settings)
        result = engine.process_next()
        self.assertEqual(result.status, "failed")
        self.assertEqual(repository.status, "failed")
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(repository.persisted, ())

    def test_provider_failure_becomes_retryable_without_chunks(self):
        provider = FakeEmbeddingProvider(
            fail_with=EmbeddingProviderUnavailable("synthetic outage")
        )
        engine, repository, _ = self.engine("Body facts.", provider=provider)
        result = engine.process_next()
        self.assertEqual(result.status, "retryable")
        self.assertEqual(repository.status, "retryable")
        self.assertEqual(repository.persisted, ())
        self.assertNotIn("synthetic outage", repository.last_error)

    def test_unexpected_provider_defect_releases_claim_and_propagates(self):
        provider = FakeEmbeddingProvider(
            fail_with=RuntimeError("synthetic programming defect")
        )
        engine, repository, _ = self.engine("Body facts.", provider=provider)
        with self.assertRaisesRegex(RuntimeError, "synthetic programming defect"):
            engine.process_next()
        self.assertEqual(repository.status, "retryable")
        self.assertIn("RuntimeError: embedding internal failed", repository.last_error)
        self.assertEqual(repository.persisted, ())

    def test_repository_error_releases_claim_and_propagates(self):
        body = "Body facts."
        document = _document("Bitcoin event", body)

        class FailingRepository(MemoryJobRepository):
            def complete_embedding_job_if_chunks_match(self, *args, **kwargs):
                del args, kwargs
                raise RuntimeError("synthetic repository defect")

        repository = FailingRepository(document, body)
        provider = FakeEmbeddingProvider()
        engine, _, _ = self.engine(body, repository=repository, provider=provider)
        with self.assertRaisesRegex(RuntimeError, "synthetic repository defect"):
            engine.process_next()
        self.assertEqual(repository.status, "retryable")
        self.assertEqual(provider.call_count, 0)

    def test_persistence_error_after_provider_call_is_recoverable_not_completed(self):
        body = "Body facts."
        document = _document("Bitcoin event", body)

        class FailingRepository(MemoryJobRepository):
            def persist_embedding_chunks_and_complete(self, *args, **kwargs):
                del args, kwargs
                self.events.append("persist")
                raise RuntimeError("synthetic persistence defect")

        repository = FailingRepository(document, body)
        provider = FakeEmbeddingProvider()
        engine, _, _ = self.engine(body, repository=repository, provider=provider)
        with self.assertRaisesRegex(RuntimeError, "synthetic persistence defect"):
            engine.process_next()
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(repository.status, "retryable")
        self.assertEqual(repository.persisted, ())
        self.assertEqual(
            repository.events,
            ["claim", "reconcile", "persist", "fail"],
        )

    def test_cleanup_error_does_not_mask_original_internal_defect(self):
        body = "Body facts."
        document = _document("Bitcoin event", body)

        class FailingRepository(MemoryJobRepository):
            def complete_embedding_job_if_chunks_match(self, *args, **kwargs):
                del args, kwargs
                raise RuntimeError("original repository defect")

            def fail_embedding_job(self, *args, **kwargs):
                del args, kwargs
                raise RuntimeError("cleanup storage outage")

        repository = FailingRepository(document, body)
        engine, _, _ = self.engine(body, repository=repository)
        with self.assertRaisesRegex(RuntimeError, "original repository defect"):
            engine.process_next()
        self.assertEqual(repository.status, "claimed")

    def test_invalid_provider_output_is_terminal_and_not_persisted(self):
        class InvalidProvider(FakeEmbeddingProvider):
            def embed(self, texts, dimensions):
                self.call_count += 1
                return EmbeddingBatch(
                    provider=self.provider_name,
                    model=self.model,
                    dimensions=dimensions,
                    vectors=((math.nan,) * dimensions,) * len(texts),
                )

        engine, repository, _ = self.engine("Body facts.", provider=InvalidProvider())
        result = engine.process_next()
        self.assertEqual(result.status, "failed")
        self.assertEqual(repository.status, "failed")
        self.assertEqual(repository.persisted, ())

    def test_matching_existing_chunks_reconcile_with_zero_provider_calls(self):
        body = "BTC rose after approval."
        engine, repository, provider = self.engine(body)
        prepared = prepare_document("Bitcoin event", body)
        repository.expected_existing_hashes = tuple(
            chunk.sha256 for chunk in prepared.chunks
        )
        result = engine.process_next()
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.reconciled)
        self.assertEqual(result.provider_calls, 0)
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(repository.persisted, ())

    def test_already_completed_job_causes_zero_provider_calls(self):
        engine, repository, provider = self.engine("Body facts.")
        repository.status = "completed"
        result = engine.process_next()
        self.assertEqual(result.status, "idle")
        self.assertEqual(provider.call_count, 0)

    def test_lost_claim_before_provider_causes_zero_provider_calls(self):
        engine, repository, provider = self.engine("Body facts.")
        repository.ownership_lost = True
        result = engine.process_next()
        self.assertEqual(result.status, "lost_claim")
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(repository.persisted, ())

    def test_content_hash_mismatch_is_terminal_before_provider(self):
        body = "Changed facts."
        document = _document("Bitcoin event", "Original facts.")
        repository = MemoryJobRepository(document, body)
        provider = FakeEmbeddingProvider()
        engine = EmbeddingJobEngine(
            repository=repository,
            provider=provider,
            settings=SETTINGS,
            content_loader=lambda _document: body,
        )
        result = engine.process_next()
        self.assertEqual(result.status, "failed")
        self.assertEqual(provider.call_count, 0)

    def test_logs_exclude_raw_content_vectors_and_full_claim_token(self):
        marker = "PRIVATE-RAW-CONTENT-987654"
        engine, _repository, _provider = self.engine(marker)
        with self.assertLogs("embeddings.service", level="INFO") as captured:
            result = engine.process_next()
        output = "\n".join(captured.output)
        self.assertEqual(result.status, "completed")
        self.assertNotIn(marker, output)
        self.assertNotIn("a" * 64, output)
        self.assertNotIn("[0.", output)

    def test_safe_errors_do_not_include_exception_messages(self):
        marker = "PRIVATE-CONTENT"
        safe = safe_embedding_error(RuntimeError(marker), "provider")
        self.assertNotIn(marker, safe)
        self.assertLessEqual(len(safe), 500)

    def test_automatic_pipeline_modules_have_no_embedding_import_or_enqueue(self):
        for relative_path in (
            "GetNewsAPI/app.py",
            "GetNewsAPI/scheduler.py",
            "GetNewsAPI/fetcher.py",
            "GetNewsAPI/gpt_processor.py",
            "GetNewsAPI/publish_to_wp.py",
        ):
            source = (REPOSITORY_DIR / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("from embeddings", source, relative_path)
            self.assertNotIn("enqueue_embedding_job", source, relative_path)


if __name__ == "__main__":
    unittest.main()
