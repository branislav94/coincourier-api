"""Opt-in semantic assessment constraints/rollback on disposable MariaDB 11.8.

Reuse the existing loopback/exact-test-schema guard and migration fixture. These
tests never query application/WordPress databases or call an embedding provider.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import unittest

from mysql.connector import Error as ConnectorError

from GetNewsAPI.tests.integration import test_vector_mariadb as native
from vector_store.models import SourceType, VectorDocumentDraft
from vector_store.repository import VectorRepository
from vector_store.semantic_assessments import (
    SemanticAssessmentDraft,
    SemanticAssessmentPersistenceError,
    SemanticAssessmentRepository,
    SemanticAssessmentStatus,
)


VERSION = "synthetic:persistence:1536:chunk-v1"
SEMANTIC_VERSION = "semantic-shadow-v1"


@unittest.skipUnless(native.RUN_INTEGRATION, "requires disposable MariaDB 11.8 vector integration")
class SemanticPersistenceMariaDBTests(unittest.TestCase):
    def setUp(self) -> None:
        native._assert_disposable_target()
        native._drop_vector_tables()
        for filename in ("001_vector_schema.sql", "002_vector_indexes.sql",
                         "003_semantic_shadow_assessments.sql"):
            native._execute_script(native.MIGRATION_DIR / filename)
        vector = VectorRepository(connect=native._connect)
        self.query_document_id = vector.upsert_document(VectorDocumentDraft(
            source_type=SourceType.SOURCE_ARTICLE, source_article_id=9100,
            rich_article_id=None, source_url="https://source.example.invalid/query",
            title="Synthetic persistence query", published_at=datetime(2026, 9, 1, 12),
            content_hash="a" * 64, content_version="synthetic-persistence-v1",
        ))
        self.repository = SemanticAssessmentRepository(connect=native._connect)

    def draft(self, status=SemanticAssessmentStatus.RETRIEVED, *, provisional=False):
        retrieved = status is SemanticAssessmentStatus.RETRIEVED
        failed = status is SemanticAssessmentStatus.ERROR
        evidence = ({
            "candidate_source_article_id": 9101,
            "candidate_document_id": self.query_document_id + 1,
            "candidate_document_key": "source_article:9101",
            "candidate_source_url": "https://source.example.invalid/candidate",
            "candidate_title": "Synthetic quoted \"candidate\" — Unicode",
            "native_distance": 0.125, "embedding_version": VERSION,
            "best_query_chunk_index": 0, "best_candidate_chunk_index": 0,
            "matched_query_chunk_count": 1, "published_at": "2026-09-01T11:00:00Z",
            "publication_delta_hours": 1.0, "source_type": "source_article",
        },) if retrieved else ()
        return SemanticAssessmentDraft(
            query_source_article_id=9100,
            query_document_id=None if provisional else self.query_document_id,
            embedding_version=VERSION, semantic_version=SEMANTIC_VERSION,
            status=status, reason="retrieval_failed" if failed else None,
            lookback_hours=72, requested_top_k=10,
            query_chunks_available=1, query_chunks_considered=1,
            best_native_distance=0.125 if retrieved else None, evidence=evidence,
            error_type="RuntimeError" if failed else None,
            safe_error="RuntimeError: retrieval failed" if failed else None,
        )

    def record(self, document_id):
        return self.repository.get_assessment(9100, document_id, VERSION, SEMANTIC_VERSION)

    def count(self):
        connection = native._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM semantic_shadow_assessments")
            return cursor.fetchone()[0]
        finally:
            cursor.close()
            connection.close()

    def test_actual_status_upserts_keep_identity_and_valid_json_shape(self):
        assessment_id = None
        statuses = (
            SemanticAssessmentStatus.DISABLED, SemanticAssessmentStatus.NOT_READY,
            SemanticAssessmentStatus.NO_CANDIDATES, SemanticAssessmentStatus.RETRIEVED,
            SemanticAssessmentStatus.ERROR, SemanticAssessmentStatus.RETRIEVED,
        )
        for evaluation, status in enumerate(statuses, 1):
            with self.subTest(status=status, evaluation=evaluation):
                saved_id = self.repository.save_assessment(self.draft(status))
                assessment_id = saved_id if assessment_id is None else assessment_id
                self.assertEqual(saved_id, assessment_id)
                record = self.record(self.query_document_id)
                self.assertEqual(record.status, status)
                self.assertEqual(record.evaluation_count, evaluation)
                self.assertEqual(record.candidate_count, len(record.evidence))
                self.assertEqual(record.candidate_count, int(status is SemanticAssessmentStatus.RETRIEVED))
                self.assertEqual(self.count(), 1)
        self.assertIn("Unicode", self.record(self.query_document_id).evidence[0]["candidate_title"])

    def test_real_check_constraints_reject_invalid_updates_without_damaging_row(self):
        assessment_id = self.repository.save_assessment(self.draft())
        invalid_updates = (
            "evidence_json='{}'", "evidence_json='[]'", "candidate_count=2",
            "status='no_candidates'", "status='error'", "requested_top_k=21",
        )
        for assignments in invalid_updates:
            with self.subTest(assignments=assignments):
                connection = native._connect()
                cursor = connection.cursor()
                try:
                    with self.assertRaises(ConnectorError):
                        cursor.execute(
                            "UPDATE semantic_shadow_assessments SET " + assignments + " WHERE id=%s",
                            (assessment_id,),
                        )
                    connection.rollback()
                finally:
                    cursor.close()
                    connection.close()
                record = self.record(self.query_document_id)
                self.assertEqual(record.status, SemanticAssessmentStatus.RETRIEVED)
                self.assertEqual(record.candidate_count, 1)
                self.assertEqual(record.evaluation_count, 1)

    def test_real_foreign_key_rejection_rolls_back_and_reports_codes_only(self):
        original = self.repository.save_assessment(self.draft(SemanticAssessmentStatus.NO_CANDIDATES))
        missing_document_id = self.query_document_id + 100000
        draft = replace(self.draft(SemanticAssessmentStatus.NO_CANDIDATES),
                        query_document_id=missing_document_id)
        with self.assertRaises(SemanticAssessmentPersistenceError) as caught:
            self.repository.save_assessment(draft)
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic.operation, "upsert")
        self.assertEqual(diagnostic.errno, 1452)
        self.assertEqual(diagnostic.sqlstate, "23000")
        self.assertEqual(diagnostic.transaction_outcome, "rolled_back")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.record(self.query_document_id).id, original)
        self.assertIsNone(self.record(missing_document_id))
        self.assertNotIn("FOREIGN KEY", str(caught.exception))

    def test_constraint_failure_after_merge_delete_restores_both_rows(self):
        # Preserve the existing identity policy: a late provisional save can
        # coexist with a resolved row until the next resolved assessment.
        resolved_id = self.repository.save_assessment(self.draft(SemanticAssessmentStatus.NO_CANDIDATES))
        provisional_id = self.repository.save_assessment(self.draft(
            SemanticAssessmentStatus.NOT_READY, provisional=True,
        ))
        self.assertEqual(self.count(), 2)

        class InvalidPromotionCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("UPDATE semantic_shadow_assessments"):
                    params = list(params)
                    # Keep valid JSON but mismatch its actual candidate count;
                    # the real MariaDB CHECK, not a mocked exception, rejects it.
                    params[7] = 2
                    params = tuple(params)
                return self.cursor.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class InvalidPromotionConnection:
            def __init__(self):
                self.connection = native._connect()

            def cursor(self, **kwargs):
                return InvalidPromotionCursor(self.connection.cursor(**kwargs))

            def __getattr__(self, name):
                return getattr(self.connection, name)

        failing = SemanticAssessmentRepository(connect=InvalidPromotionConnection)
        with self.assertRaises(SemanticAssessmentPersistenceError) as caught:
            failing.save_assessment(self.draft())
        self.assertEqual(caught.exception.diagnostic.operation, "promote_provisional")
        self.assertEqual(caught.exception.diagnostic.errno, 4025)
        self.assertEqual(caught.exception.diagnostic.transaction_outcome, "rolled_back")
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.record(None).id, provisional_id)
        self.assertEqual(self.record(None).evaluation_count, 1)
        self.assertEqual(self.record(self.query_document_id).id, resolved_id)
        self.assertEqual(self.record(self.query_document_id).evaluation_count, 1)

        # A normal subsequent save still performs the unchanged atomic merge.
        self.assertEqual(self.repository.save_assessment(self.draft()), provisional_id)
        merged = self.record(self.query_document_id)
        self.assertEqual(merged.evaluation_count, 3)
        self.assertEqual(merged.candidate_count, 1)
        self.assertEqual(self.count(), 1)

