"""Opt-in Phase 6B1 job-engine coverage on disposable MariaDB 11.8."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_DIR = PROJECT_DIR.parent
MIGRATION_DIR = REPOSITORY_DIR / "maintenance" / "vector_migrations"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from embeddings.chunking import prepare_document
from embeddings.models import EmbeddingBatch, EmbeddingSettings
from embeddings.provider import (
    EmbeddingProviderUnavailable,
    FakeEmbeddingProvider,
)
from embeddings.service import EmbeddingJobEngine
from vector_store.models import (
    SourceType,
    VECTOR_DIMENSIONS,
    VectorChunkWrite,
    VectorDocumentDraft,
)
from vector_store.repository import VectorRepository


RUN_INTEGRATION = os.getenv("RUN_VECTOR_MARIADB_INTEGRATION", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DB_HOST = os.getenv("VECTOR_MARIADB_TEST_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("VECTOR_MARIADB_TEST_PORT", "13309"))
DB_NAME = os.getenv("VECTOR_MARIADB_TEST_DATABASE", "coincourier_vectors_test")
DB_USER = os.getenv("VECTOR_MARIADB_TEST_USER", "vector_test")
DB_PASSWORD = os.getenv("VECTOR_MARIADB_TEST_PASSWORD", "vector_test_only")


def _assert_disposable_target() -> None:
    if DB_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("embedding integration tests require a loopback host")
    if DB_NAME != "coincourier_vectors_test":
        raise RuntimeError("embedding integration tests require coincourier_vectors_test")


def _connect(*, autocommit: bool = False):
    _assert_disposable_target()
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        autocommit=autocommit,
        connection_timeout=10,
    )


def _execute_script(path: Path) -> None:
    connection = _connect(autocommit=True)
    cursor = connection.cursor()
    try:
        cursor.execute(path.read_text(encoding="utf-8"), map_results=True)
        while cursor.nextset():
            pass
    finally:
        cursor.close()
        connection.close()


def _drop_vector_tables() -> None:
    connection = _connect(autocommit=True)
    cursor = connection.cursor()
    try:
        for table in ("embedding_jobs", "vector_chunks", "vector_documents"):
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        cursor.close()
        connection.close()


def _settings(
    *,
    model: str = "text-embedding-3-small",
    batch_size: int = 16,
) -> EmbeddingSettings:
    return EmbeddingSettings(
        enabled=True,
        provider="openai",
        model=model,
        dimensions=VECTOR_DIMENSIONS,
        chunker_version="chunk-v1",
        batch_size=batch_size,
    )


@unittest.skipUnless(
    RUN_INTEGRATION,
    "set RUN_VECTOR_MARIADB_INTEGRATION=true for disposable MariaDB 11.8 tests",
)
class EmbeddingMariaDBIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _drop_vector_tables()
        _execute_script(MIGRATION_DIR / "001_vector_schema.sql")
        _execute_script(MIGRATION_DIR / "002_vector_indexes.sql")
        self.repository = VectorRepository(connect=_connect)
        self.bodies: dict[int, str] = {}

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        connection = _connect()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return cursor.fetchone()[0]
        finally:
            cursor.close()
            connection.close()

    def register(
        self,
        source_article_id: int,
        *,
        title: str,
        body: str,
    ) -> tuple[int, Any]:
        prepared = prepare_document(title, body)
        document_id = self.repository.upsert_document(
            VectorDocumentDraft(
                source_type=SourceType.SOURCE_ARTICLE,
                source_article_id=source_article_id,
                rich_article_id=None,
                source_url=f"https://source.example.test/{source_article_id}",
                title=title,
                published_at=None,
                content_hash=prepared.content_hash,
                content_version=prepared.content_version,
            )
        )
        self.bodies[document_id] = body
        return document_id, prepared

    def engine(self, provider, settings):
        return EmbeddingJobEngine(
            repository=VectorRepository(connect=_connect),
            provider=provider,
            settings=settings,
            content_loader=lambda document: self.bodies[document.id],
        )

    def test_enqueue_claim_ownership_and_attempt_count(self) -> None:
        settings = _settings()
        document_id, prepared = self.register(1, title="Ownership", body="Body facts.")
        job_id = self.repository.enqueue_embedding_job(
            document_id, settings.embedding_version
        )
        self.assertEqual(
            self.repository.enqueue_embedding_job(document_id, settings.embedding_version),
            job_id,
        )
        claim = self.repository.claim_embedding_job(
            settings.embedding_version, timeout_minutes=30
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.attempt, 1)
        self.assertEqual(
            self.repository.persist_embedding_chunks_and_complete(
                job_id,
                "wrong-owner",
                chunks=(
                    VectorChunkWrite(
                        chunk_index=0,
                        chunk_text=prepared.chunks[0].text,
                        chunk_hash=prepared.chunks[0].sha256,
                        embedding=FakeEmbeddingProvider().embed(
                            [prepared.chunks[0].text], VECTOR_DIMENSIONS
                        ).vectors[0],
                    ),
                ),
                embedding_model=settings.model,
                embedding_version=settings.embedding_version,
            ),
            False,
        )
        self.assertFalse(
            self.repository.fail_embedding_job(
                job_id,
                "wrong-owner",
                "RuntimeError: embedding provider failed",
                terminal=False,
            )
        )
        self.assertTrue(
            self.repository.fail_embedding_job(
                job_id,
                claim.token,
                "RuntimeError: embedding provider failed",
                terminal=False,
            )
        )
        retry = self.repository.claim_embedding_job(
            settings.embedding_version, timeout_minutes=30
        )
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.attempt, 2)

    def test_two_workers_have_one_owner(self) -> None:
        settings = _settings()
        document_id, _prepared = self.register(1, title="Race", body="One job.")
        self.repository.enqueue_embedding_job(document_id, settings.embedding_version)
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            return VectorRepository(connect=_connect).claim_embedding_job(
                settings.embedding_version,
                timeout_minutes=30,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _index: claim(), range(2)))
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM embedding_jobs WHERE status='claimed'"),
            1,
        )
        self.assertEqual(
            self.scalar("SELECT COUNT(DISTINCT claim_token) FROM embedding_jobs"),
            1,
        )

    def test_expired_claim_is_recovered_with_new_token(self) -> None:
        settings = _settings()
        document_id, _prepared = self.register(1, title="Expiry", body="One job.")
        job_id = self.repository.enqueue_embedding_job(
            document_id, settings.embedding_version
        )
        first = self.repository.claim_embedding_job(
            settings.embedding_version, timeout_minutes=30
        )
        self.assertIsNotNone(first)
        assert first is not None
        connection = _connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE embedding_jobs
                SET claimed_at=TIMESTAMPADD(MINUTE,-31,UTC_TIMESTAMP())
                WHERE id=%s
                """,
                (job_id,),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        recovered = self.repository.claim_embedding_job(
            settings.embedding_version, timeout_minutes=30
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertTrue(recovered.recovered)
        self.assertEqual(recovered.attempt, 2)
        self.assertNotEqual(recovered.token, first.token)

    def test_provider_runs_without_claim_lock_and_completion_is_atomic(self) -> None:
        settings = _settings()
        document_id, prepared = self.register(
            1,
            title="Transaction boundary",
            body="BTC rose 12% after the approval.",
        )
        job_id = self.repository.enqueue_embedding_job(
            document_id, settings.embedding_version
        )

        class ObservingProvider(FakeEmbeddingProvider):
            def embed(inner_self, texts, dimensions):
                observer = _connect()
                cursor = observer.cursor()
                try:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout=1")
                    observer.start_transaction()
                    cursor.execute(
                        "SELECT status FROM embedding_jobs WHERE id=%s FOR UPDATE",
                        (job_id,),
                    )
                    self.assertEqual(cursor.fetchone(), ("claimed",))
                    observer.rollback()
                finally:
                    cursor.close()
                    observer.close()
                return super().embed(texts, dimensions)

        provider = ObservingProvider()
        result = self.engine(provider, settings).process_next()
        self.assertEqual(result.status, "completed")
        self.assertEqual(provider.call_count, 1)
        job = self.repository.get_embedding_job(job_id)
        self.assertEqual(job.status, "completed")
        self.assertIsNone(job.claim_token)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM vector_chunks WHERE document_id=%s",
                (document_id,),
            ),
            len(prepared.chunks),
        )

    def test_provider_failure_and_invalid_output_persist_no_chunks(self) -> None:
        settings = _settings()
        first_id, _first = self.register(1, title="Transient", body="Retry this.")
        first_job = self.repository.enqueue_embedding_job(
            first_id, settings.embedding_version
        )
        transient = FakeEmbeddingProvider(
            fail_with=EmbeddingProviderUnavailable("synthetic outage")
        )
        result = self.engine(transient, settings).process_next()
        self.assertEqual(result.status, "retryable")
        self.assertEqual(self.repository.get_embedding_job(first_job).status, "retryable")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM vector_chunks"), 0)

        retry_claim = self.repository.claim_embedding_job(
            settings.embedding_version,
            timeout_minutes=30,
        )
        self.assertIsNotNone(retry_claim)
        assert retry_claim is not None
        self.assertTrue(
            self.repository.fail_embedding_job(
                first_job,
                retry_claim.token,
                "InvalidEmbeddingInput: embedding validation failed",
                terminal=True,
            )
        )

        second_id, _second = self.register(2, title="Invalid", body="Reject this.")
        second_job = self.repository.enqueue_embedding_job(
            second_id, settings.embedding_version
        )

        class InvalidProvider(FakeEmbeddingProvider):
            def embed(self, texts, dimensions):
                self.call_count += 1
                return EmbeddingBatch(
                    provider=self.provider_name,
                    model=self.model,
                    dimensions=dimensions,
                    vectors=((float("nan"),) * dimensions,) * len(texts),
                )

        invalid_result = self.engine(InvalidProvider(), settings).process_next()
        self.assertEqual(invalid_result.status, "failed")
        self.assertEqual(self.repository.get_embedding_job(second_job).status, "failed")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM vector_chunks"), 0)

    def test_matching_chunks_reconcile_without_provider_call(self) -> None:
        settings = _settings()
        document_id, prepared = self.register(
            1,
            title="Reconcile",
            body="Persisted vectors already exist.",
        )
        vectors = FakeEmbeddingProvider().embed(
            [chunk.text for chunk in prepared.chunks], VECTOR_DIMENSIONS
        ).vectors
        for chunk, vector in zip(prepared.chunks, vectors, strict=True):
            self.repository.upsert_chunk(
                document_id=document_id,
                chunk_index=chunk.index,
                chunk_text=chunk.text,
                embedding=vector,
                embedding_model=settings.model,
                embedding_version=settings.embedding_version,
            )
        job_id = self.repository.enqueue_embedding_job(
            document_id, settings.embedding_version
        )
        provider = FakeEmbeddingProvider()
        result = self.engine(provider, settings).process_next()
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.reconciled)
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(self.repository.get_embedding_job(job_id).status, "completed")

    def test_partial_retry_is_replaced_without_duplicate_chunks(self) -> None:
        settings = _settings(batch_size=2)
        body = " ".join(f"token{index}" for index in range(1300))
        document_id, prepared = self.register(1, title="Partial", body=body)
        self.assertGreater(len(prepared.chunks), 1)
        first_vector = FakeEmbeddingProvider().embed(
            [prepared.chunks[0].text], VECTOR_DIMENSIONS
        ).vectors[0]
        self.repository.upsert_chunk(
            document_id=document_id,
            chunk_index=0,
            chunk_text=prepared.chunks[0].text,
            embedding=first_vector,
            embedding_model=settings.model,
            embedding_version=settings.embedding_version,
        )
        self.repository.enqueue_embedding_job(document_id, settings.embedding_version)
        provider = FakeEmbeddingProvider()
        result = self.engine(provider, settings).process_next()
        self.assertEqual(result.status, "completed")
        self.assertGreater(provider.call_count, 0)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM vector_chunks WHERE document_id=%s",
                (document_id,),
            ),
            len(prepared.chunks),
        )
        self.assertEqual(
            self.scalar(
                """
                SELECT COUNT(DISTINCT chunk_index)
                FROM vector_chunks WHERE document_id=%s
                """,
                (document_id,),
            ),
            len(prepared.chunks),
        )

    def test_atomic_failure_restores_previous_chunks_and_keeps_claim(self) -> None:
        settings = _settings()
        document_id, prepared = self.register(1, title="Atomic", body="Original chunk.")
        vector = FakeEmbeddingProvider().embed(
            [prepared.chunks[0].text], VECTOR_DIMENSIONS
        ).vectors[0]
        self.repository.upsert_chunk(
            document_id=document_id,
            chunk_index=0,
            chunk_text=prepared.chunks[0].text,
            embedding=vector,
            embedding_model=settings.model,
            embedding_version=settings.embedding_version,
        )
        job_id = self.repository.enqueue_embedding_job(
            document_id, settings.embedding_version
        )
        claim = self.repository.claim_embedding_job(
            settings.embedding_version, timeout_minutes=30
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        duplicate_hash = hashlib.sha256(b"duplicate").hexdigest()
        writes = (
            VectorChunkWrite(0, "duplicate", duplicate_hash, vector),
            VectorChunkWrite(1, "duplicate", duplicate_hash, vector),
        )
        with self.assertRaises(mysql.connector.Error):
            self.repository.persist_embedding_chunks_and_complete(
                job_id,
                claim.token,
                chunks=writes,
                embedding_model=settings.model,
                embedding_version=settings.embedding_version,
            )
        chunks = self.repository.get_chunks(
            document_id,
            embedding_version=settings.embedding_version,
        )
        self.assertEqual([chunk.chunk_text for chunk in chunks], [prepared.chunks[0].text])
        job = self.repository.get_embedding_job(job_id)
        self.assertEqual(job.status, "claimed")
        self.assertEqual(job.claim_token, claim.token)

    def test_completed_job_zero_calls_changed_content_and_versions_coexist(self) -> None:
        settings = _settings()
        document_id, first = self.register(1, title="Versioned", body="Initial facts.")
        first_job = self.repository.enqueue_embedding_job(
            document_id, settings.embedding_version
        )
        first_provider = FakeEmbeddingProvider()
        self.assertEqual(
            self.engine(first_provider, settings).process_next().status,
            "completed",
        )
        self.assertEqual(first_provider.call_count, 1)
        self.repository.enqueue_embedding_job(document_id, settings.embedding_version)
        self.assertEqual(
            self.engine(first_provider, settings).process_next().status,
            "idle",
        )
        self.assertEqual(first_provider.call_count, 1)
        self.assertEqual(self.repository.get_embedding_job(first_job).status, "completed")

        changed_id, changed = self.register(
            1,
            title="Versioned",
            body="Initial facts plus a material update.",
        )
        self.assertNotEqual(changed_id, document_id)
        self.assertNotEqual(changed.content_version, first.content_version)

        compatible = _settings(model="text-embedding-3-small-compatible-v2")
        self.repository.enqueue_embedding_job(document_id, compatible.embedding_version)
        compatible_provider = FakeEmbeddingProvider(model=compatible.model)
        self.assertEqual(
            self.engine(compatible_provider, compatible).process_next().status,
            "completed",
        )
        self.assertEqual(
            self.scalar(
                """
                SELECT COUNT(DISTINCT embedding_version)
                FROM vector_chunks WHERE document_id=%s
                """,
                (document_id,),
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
