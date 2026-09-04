"""Opt-in native VECTOR coverage against disposable MariaDB 11.8."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_DIR = PROJECT_DIR.parent
MIGRATION_DIR = REPOSITORY_DIR / "maintenance" / "vector_migrations"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from vector_store.db import VectorSchemaError, verify_vector_schema
from vector_store.models import SourceType, VECTOR_DIMENSIONS, VectorDocumentDraft
from vector_store.repository import VectorRepository, serialize_embedding
from semantic_retrieval.evaluation import (
    EvaluationFixture,
    LabeledRelationship,
    RelationshipLabel,
    RelevanceDefinition,
    evaluate_retrieval,
)
from semantic_retrieval.models import SemanticRetrievalSettings, SemanticRetrievalStatus
from semantic_retrieval.service import SemanticRetrievalService


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
        raise RuntimeError("vector integration tests require a loopback host")
    if DB_NAME != "coincourier_vectors_test":
        raise RuntimeError("vector integration tests require coincourier_vectors_test")


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


def fake_vector(first: float, second: float = 0.0) -> list[float]:
    vector = [0.0] * VECTOR_DIMENSIONS
    vector[0] = first
    vector[1] = second
    return vector


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@unittest.skipUnless(
    RUN_INTEGRATION,
    "set RUN_VECTOR_MARIADB_INTEGRATION=true for disposable MariaDB 11.8 tests",
)
class VectorMariaDBIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _drop_vector_tables()
        self.apply("001_vector_schema.sql")
        self.apply("002_vector_indexes.sql")
        self.repository = VectorRepository(connect=_connect)

    def apply(self, filename: str) -> None:
        _execute_script(MIGRATION_DIR / filename)

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        connection = _connect()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return cursor.fetchone()[0]
        finally:
            cursor.close()
            connection.close()

    def source_document(
        self,
        article_id: int,
        *,
        title: str,
        published_at: datetime | None = None,
        content_version: str = "source-v1",
    ) -> VectorDocumentDraft:
        return VectorDocumentDraft(
            source_type=SourceType.SOURCE_ARTICLE,
            source_article_id=article_id,
            rich_article_id=None,
            source_url=f"https://source.example.test/{article_id}",
            title=title,
            published_at=published_at,
            content_hash=digest(f"{article_id}:{content_version}:{title}"),
            content_version=content_version,
        )

    def add_chunk(
        self,
        document_id: int,
        *,
        vector: list[float],
        text: str,
        chunk_index: int = 0,
        version: str = "synthetic:test:1536:chunk-v1",
    ) -> None:
        self.repository.upsert_chunk(
            document_id=document_id,
            chunk_index=chunk_index,
            chunk_text=text,
            embedding=vector,
            embedding_model="synthetic:test",
            embedding_version=version,
        )

    def mark_embedding_complete(
        self,
        document_id: int,
        *,
        version: str = "synthetic:test:1536:chunk-v1",
    ) -> None:
        job_id = self.repository.enqueue_embedding_job(document_id, version)
        connection = _connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE embedding_jobs SET status='completed' WHERE id=%s",
                (job_id,),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    def test_001_clean_migrations_schema_and_rerun(self) -> None:
        connection = _connect()
        try:
            verify_vector_schema(connection)
        finally:
            connection.close()
        self.assertTrue(str(self.scalar("SELECT VERSION()")).startswith("11.8."))
        self.assertEqual(
            self.scalar(
                """
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME IN ('vector_documents','vector_chunks','embedding_jobs')
                """
            ),
            3,
        )
        self.assertEqual(
            self.scalar(
                """
                SELECT COLUMN_TYPE FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='vector_chunks'
                  AND COLUMN_NAME='embedding'
                """
            ).lower(),
            "vector(1536)",
        )
        self.assertEqual(
            self.scalar(
                """
                SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA=DATABASE()
                  AND CONSTRAINT_NAME IN (
                      'fk_vector_chunks_document', 'fk_embedding_jobs_document'
                  )
                """
            ),
            2,
        )
        self.assertEqual(
            self.scalar(
                """
                SELECT COUNT(DISTINCT INDEX_NAME)
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND INDEX_NAME IN (
                      'uq_vector_documents_key_version',
                      'uq_vector_chunks_position_version',
                      'uq_vector_chunks_hash_version',
                      'uq_embedding_jobs_document_version'
                  )
                """
            ),
            4,
        )
        self.assertEqual(
            self.scalar(
                """
                SELECT
                    (SELECT COUNT(*) FROM vector_documents)
                  + (SELECT COUNT(*) FROM vector_chunks)
                  + (SELECT COUNT(*) FROM embedding_jobs)
                """
            ),
            0,
        )
        self.apply("001_vector_schema.sql")
        self.apply("002_vector_indexes.sql")
        connection = _connect()
        try:
            verify_vector_schema(connection)
        finally:
            connection.close()

    def test_002_incompatible_existing_schema_is_rejected(self) -> None:
        _drop_vector_tables()
        connection = _connect(autocommit=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                "CREATE TABLE vector_documents (id BIGINT UNSIGNED PRIMARY KEY) ENGINE=InnoDB"
            )
        finally:
            cursor.close()
            connection.close()
        self.apply("001_vector_schema.sql")
        self.apply("002_vector_indexes.sql")
        connection = _connect()
        try:
            with self.assertRaises(VectorSchemaError):
                verify_vector_schema(connection)
        finally:
            connection.close()

    def test_vector_dimension_malformed_input_and_transaction_rollback(self) -> None:
        connection = _connect()
        cursor = connection.cursor()
        document_id = self.repository.upsert_document(
            self.source_document(1, title="Dimension probe")
        )
        try:
            with self.assertRaises(mysql.connector.Error) as short_error:
                cursor.execute(
                    """
                    INSERT INTO vector_chunks
                        (document_id,chunk_index,chunk_text,chunk_hash,embedding,
                         embedding_model,embedding_dimensions,embedding_version)
                    VALUES (%s,0,'short',%s,VEC_FromText(%s),'synthetic',1536,'v1')
                    """,
                    (document_id, digest("short"), json.dumps(fake_vector(1.0)[:-1])),
                )
            self.assertEqual(short_error.exception.errno, 1292)
            connection.rollback()

            with self.assertRaises(mysql.connector.Error) as malformed_error:
                cursor.execute(
                    """
                    INSERT INTO vector_chunks
                        (document_id,chunk_index,chunk_text,chunk_hash,embedding,
                         embedding_model,embedding_dimensions,embedding_version)
                    VALUES (%s,0,'malformed',%s,VEC_FromText('not-a-vector'),
                            'synthetic',1536,'malformed-v1')
                    """,
                    (document_id, digest("malformed")),
                )
            self.assertEqual(malformed_error.exception.errno, 4038)
            connection.rollback()

            connection.start_transaction()
            cursor.execute(
                """
                INSERT INTO vector_chunks
                    (document_id,chunk_index,chunk_text,chunk_hash,embedding,
                     embedding_model,embedding_dimensions,embedding_version)
                VALUES (%s,0,'rollback',%s,VEC_FromText(%s),'synthetic',1536,'rollback-v1')
                """,
                (document_id, digest("rollback"), serialize_embedding(fake_vector(1.0))),
            )
            connection.rollback()
            cursor.execute("SELECT COUNT(*) FROM vector_chunks")
            self.assertEqual(cursor.fetchone(), (0,))
        finally:
            cursor.close()
            connection.close()

    def test_document_provenance_and_idempotency(self) -> None:
        source = self.source_document(1, title="Source")
        first_id = self.repository.upsert_document(source)
        self.assertEqual(self.repository.upsert_document(source), first_id)
        generated = VectorDocumentDraft(
            source_type=SourceType.COINCOURIER_GENERATED,
            source_article_id=1,
            rich_article_id=101,
            source_url=source.source_url,
            title="CoinCourier derivative",
            published_at=datetime.now(UTC).replace(tzinfo=None),
            content_hash=digest("generated-101"),
            content_version="rich-v1",
        )
        generated_id = self.repository.upsert_document(generated)
        self.assertNotEqual(first_id, generated_id)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM vector_documents"), 2)
        self.assertEqual(
            self.repository.get_document(generated_id).source_article_id,
            1,
        )

    def test_chunk_round_trip_idempotency_and_multiple_versions(self) -> None:
        document_id = self.repository.upsert_document(
            self.source_document(1, title="Chunk versions")
        )
        chunk_id = self.repository.upsert_chunk(
            document_id=document_id,
            chunk_index=0,
            chunk_text="Synthetic retrieval text.",
            embedding=fake_vector(1.0),
            embedding_model="synthetic:test",
            embedding_version="synthetic:test:1536:chunk-v1",
        )
        self.assertEqual(
            self.repository.upsert_chunk(
                document_id=document_id,
                chunk_index=0,
                chunk_text="Synthetic retrieval text.",
                embedding=fake_vector(1.0),
                embedding_model="synthetic:test",
                embedding_version="synthetic:test:1536:chunk-v1",
            ),
            chunk_id,
        )
        self.repository.upsert_chunk(
            document_id=document_id,
            chunk_index=0,
            chunk_text="Synthetic retrieval text.",
            embedding=fake_vector(0.0, 1.0),
            embedding_model="synthetic:test-v2",
            embedding_version="synthetic:test-v2:1536:chunk-v1",
        )
        chunks = self.repository.get_chunks(document_id)
        self.assertEqual(len(chunks), 2)
        chunks_by_version = {chunk.embedding_version: chunk for chunk in chunks}
        first_version = chunks_by_version["synthetic:test:1536:chunk-v1"]
        self.assertEqual(len(first_version.embedding), VECTOR_DIMENSIONS)
        self.assertEqual(first_version.embedding[0], 1.0)

    def test_native_nearest_neighbor_ordering_filters_and_top_k(self) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        near_id = self.repository.upsert_document(
            self.source_document(1, title="Near source", published_at=now)
        )
        far_id = self.repository.upsert_document(
            self.source_document(2, title="Far source", published_at=now - timedelta(days=30))
        )
        generated_id = self.repository.upsert_document(
            VectorDocumentDraft(
                source_type=SourceType.COINCOURIER_GENERATED,
                source_article_id=1,
                rich_article_id=101,
                source_url="https://source.example.test/1",
                title="Generated exact match",
                published_at=now,
                content_hash=digest("generated-exact"),
                content_version="rich-v1",
            )
        )
        version = "synthetic:test:1536:chunk-v1"
        for document_id, text, vector in (
            (near_id, "Near source chunk", fake_vector(0.99, 0.01)),
            (far_id, "Far source chunk", fake_vector(-1.0)),
            (generated_id, "Generated chunk", fake_vector(1.0)),
        ):
            self.repository.upsert_chunk(
                document_id=document_id,
                chunk_index=0,
                chunk_text=text,
                embedding=vector,
                embedding_model="synthetic:test",
                embedding_version=version,
            )

        all_matches = self.repository.nearest_chunks(
            fake_vector(1.0), top_k=3, embedding_version=version
        )
        self.assertEqual(
            [match.source_type for match in all_matches],
            [
                SourceType.COINCOURIER_GENERATED,
                SourceType.SOURCE_ARTICLE,
                SourceType.SOURCE_ARTICLE,
            ],
        )
        self.assertLess(all_matches[0].distance, all_matches[1].distance)
        self.assertLess(all_matches[1].distance, all_matches[2].distance)

        source_matches = self.repository.nearest_chunks(
            fake_vector(1.0),
            top_k=2,
            embedding_version=version,
            source_type=SourceType.SOURCE_ARTICLE,
        )
        self.assertEqual([match.document_id for match in source_matches], [near_id, far_id])
        recent = self.repository.nearest_chunks(
            fake_vector(1.0),
            top_k=3,
            embedding_version=version,
            source_type=SourceType.SOURCE_ARTICLE,
            published_after=now - timedelta(days=1),
        )
        self.assertEqual([match.document_id for match in recent], [near_id])

    def test_embedding_job_storage_is_idempotent(self) -> None:
        document_id = self.repository.upsert_document(
            self.source_document(1, title="Job storage")
        )
        first = self.repository.enqueue_embedding_job(document_id, "synthetic:v1")
        second = self.repository.enqueue_embedding_job(document_id, "synthetic:v1")
        self.assertEqual(first, second)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM embedding_jobs"), 1)
        self.assertEqual(self.scalar("SELECT status FROM embedding_jobs"), "pending")

    def test_vector_index_and_explain_use_native_cosine_plan(self) -> None:
        document_id = self.repository.upsert_document(
            self.source_document(1, title="Explain probe")
        )
        version = "synthetic:test:1536:chunk-v1"
        for chunk_index, vector in enumerate(
            (fake_vector(1.0), fake_vector(0.99, 0.01), fake_vector(-1.0))
        ):
            self.repository.upsert_chunk(
                document_id=document_id,
                chunk_index=chunk_index,
                chunk_text=f"Explain chunk {chunk_index}",
                embedding=vector,
                embedding_model="synthetic:test",
                embedding_version=version,
            )
        connection = _connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                EXPLAIN
                SELECT STRAIGHT_JOIN c.id,
                       VEC_DISTANCE_COSINE(c.embedding,VEC_FromText(%s)) AS distance
                FROM vector_chunks c
                JOIN vector_documents d ON d.id=c.document_id
                WHERE c.embedding_version=%s
                ORDER BY distance LIMIT 2
                """,
                (serialize_embedding(fake_vector(1.0)), version),
            )
            plan = cursor.fetchall()
        finally:
            cursor.close()
            connection.close()
        chunk_plan = next(row for row in plan if row["table"] == "c")
        self.assertEqual(chunk_plan["key"], "idx_vector_chunks_embedding_cosine")

    def test_semantic_source_only_temporal_distinct_retrieval_and_evaluation(self) -> None:
        query_time = datetime(2026, 9, 1, 12, 0, 0)
        version = "synthetic:test:1536:chunk-v1"

        old_query_version_id = self.repository.upsert_document(
            self.source_document(
                900,
                title="Old immutable query version",
                published_at=query_time - timedelta(hours=1),
                content_version="source-old",
            )
        )
        self.add_chunk(
            old_query_version_id,
            vector=fake_vector(1.0),
            text="Same-source historical version",
            version=version,
        )
        query_id = self.repository.upsert_document(
            self.source_document(
                900,
                title="Current query article",
                published_at=query_time,
                content_version="source-current",
            )
        )
        self.add_chunk(
            query_id,
            vector=fake_vector(1.0),
            text="Query event",
            version=version,
        )
        self.mark_embedding_complete(query_id, version=version)

        candidates = []
        for article_id, title, age_hours, vector in (
            (901, "Candidate A", 1, fake_vector(0.99, 0.01)),
            (902, "Candidate B", 2, fake_vector(0.8, 0.2)),
            (903, "Candidate C", 3, fake_vector(0.0, 1.0)),
        ):
            candidate_id = self.repository.upsert_document(
                self.source_document(
                    article_id,
                    title=title,
                    published_at=query_time - timedelta(hours=age_hours),
                )
            )
            self.add_chunk(
                candidate_id,
                vector=vector,
                text=f"{title} primary chunk",
                version=version,
            )
            candidates.append(candidate_id)
        self.add_chunk(
            candidates[0],
            vector=fake_vector(0.98, 0.02),
            text="Candidate A second matching chunk",
            chunk_index=1,
            version=version,
        )

        generated_id = self.repository.upsert_document(
            VectorDocumentDraft(
                source_type=SourceType.COINCOURIER_GENERATED,
                source_article_id=901,
                rich_article_id=1901,
                source_url="https://source.example.test/901",
                title="Derivative exact match",
                published_at=query_time - timedelta(minutes=30),
                content_hash=digest("generated-semantic"),
                content_version="generated-semantic-v1",
            )
        )
        self.add_chunk(
            generated_id,
            vector=fake_vector(1.0),
            text="Derivative exact chunk",
            version=version,
        )

        for article_id, title, published_at in (
            (904, "Future exact match", query_time + timedelta(seconds=1)),
            (905, "Outside lookback exact match", query_time - timedelta(hours=73)),
            (906, "Null-date exact match", None),
        ):
            document_id = self.repository.upsert_document(
                self.source_document(
                    article_id,
                    title=title,
                    published_at=published_at,
                )
            )
            self.add_chunk(
                document_id,
                vector=fake_vector(1.0),
                text=title,
                version=version,
            )

        incompatible_id = self.repository.upsert_document(
            self.source_document(
                907,
                title="Incompatible embedding version",
                published_at=query_time - timedelta(hours=1),
            )
        )
        self.add_chunk(
            incompatible_id,
            vector=fake_vector(1.0),
            text="Incompatible version chunk",
            version="synthetic:other:1536:chunk-v1",
        )

        semantic = SemanticRetrievalService(
            repository=self.repository,
            settings=SemanticRetrievalSettings(
                vector_enabled=True,
                semantic_enabled=True,
                embedding_version=version,
                lookback_hours=72,
                top_k=3,
            ),
        )
        before_counts = (
            self.scalar("SELECT COUNT(*) FROM vector_documents"),
            self.scalar("SELECT COUNT(*) FROM vector_chunks"),
            self.scalar("SELECT COUNT(*) FROM embedding_jobs"),
        )
        result = semantic.retrieve_source_neighbors(900)
        self.assertEqual(result.status, SemanticRetrievalStatus.RETRIEVED)
        self.assertEqual(
            [item.candidate_source_article_id for item in result.candidates],
            [901, 902, 903],
        )
        self.assertEqual(len({item.candidate_source_article_id for item in result.candidates}), 3)
        self.assertEqual(result.candidates[0].best_candidate_chunk_index, 0)
        self.assertLess(result.candidates[0].native_distance, result.candidates[1].native_distance)
        self.assertLess(result.candidates[1].native_distance, result.candidates[2].native_distance)

        fixture = EvaluationFixture(
            schema_version="semantic-eval-v1",
            relationships=(
                LabeledRelationship(900, 901, RelationshipLabel.EXACT_DUPLICATE),
                LabeledRelationship(900, 902, RelationshipLabel.MATERIAL_UPDATE),
                LabeledRelationship(900, 903, RelationshipLabel.UNRELATED),
            ),
        )
        metrics = evaluate_retrieval(
            fixture,
            semantic,
            top_k=3,
            relevance_definition=RelevanceDefinition.STRICT_DUPLICATE,
        )
        self.assertEqual(metrics.recall_at_k, 1.0)
        self.assertEqual(metrics.mean_reciprocal_rank, 1.0)
        self.assertEqual(
            before_counts,
            (
                self.scalar("SELECT COUNT(*) FROM vector_documents"),
                self.scalar("SELECT COUNT(*) FROM vector_chunks"),
                self.scalar("SELECT COUNT(*) FROM embedding_jobs"),
            ),
        )

    def test_semantic_filtered_explain_uses_cosine_vector_index(self) -> None:
        query_time = datetime(2026, 9, 1, 12, 0, 0)
        document_id = self.repository.upsert_document(
            self.source_document(950, title="Semantic explain", published_at=query_time)
        )
        version = "synthetic:test:1536:chunk-v1"
        self.add_chunk(
            document_id,
            vector=fake_vector(1.0),
            text="Semantic explain chunk",
            version=version,
        )
        connection = _connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                EXPLAIN
                SELECT id,
                       VEC_DISTANCE_COSINE(
                           embedding, VEC_FromText(%s)
                       ) AS distance
                FROM vector_chunks
                WHERE embedding_version=%s
                ORDER BY distance ASC
                LIMIT 15
                """,
                (
                    serialize_embedding(fake_vector(1.0)),
                    version,
                ),
            )
            plan = cursor.fetchall()
        finally:
            cursor.close()
            connection.close()
        chunk_plan = next(row for row in plan if row["table"] == "vector_chunks")
        self.assertEqual(chunk_plan["key"], "idx_vector_chunks_embedding_cosine")


if __name__ == "__main__":
    unittest.main(verbosity=2)
