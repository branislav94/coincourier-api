from __future__ import annotations

import inspect
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
MIGRATION_PATH = (
    REPOSITORY_DIR
    / "maintenance"
    / "vector_migrations"
    / "003_semantic_shadow_assessments.sql"
)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config
import gpt_processor
import semantic_retrieval.shadow as shadow
from semantic_retrieval.models import (
    SemanticCandidate,
    SemanticRetrievalResult,
    SemanticRetrievalSettings,
    SemanticRetrievalStatus,
)
from semantic_retrieval.shadow import run_semantic_shadow
from vector_store.models import (
    EmbeddingJobRecord,
    SourceType,
    VectorChunkRecord,
    VectorDocumentRecord,
    VectorMatch,
)
from vector_store.semantic_assessments import (
    SemanticAssessmentDraft,
    SemanticAssessmentRepository,
    SemanticAssessmentStatus,
)


VERSION = "openai:text-embedding-3-small:1536:chunk-v1"
SEMANTIC_VERSION = "semantic-shadow-v1"
QUERY_TIME = datetime(2026, 9, 1, 12, 0, 0)
SETTINGS = SemanticRetrievalSettings(
    vector_enabled=True,
    semantic_enabled=True,
    embedding_version=VERSION,
    lookback_hours=72,
    top_k=10,
)


def candidate(
    source_article_id: int = 201,
    *,
    document_id: int = 2010,
    distance: float = 0.125,
    source_type: SourceType = SourceType.SOURCE_ARTICLE,
    embedding_version: str = VERSION,
) -> SemanticCandidate:
    return SemanticCandidate(
        query_document_id=10,
        query_source_article_id=100,
        candidate_document_id=document_id,
        candidate_source_article_id=source_article_id,
        candidate_document_key=f"source_article:{source_article_id}",
        candidate_source_url=f"https://source.example.test/{source_article_id}",
        candidate_title=f"Candidate {source_article_id}",
        native_distance=distance,
        embedding_version=embedding_version,
        best_query_chunk_index=1,
        best_candidate_chunk_index=2,
        matched_query_chunk_count=3,
        published_at=QUERY_TIME - timedelta(hours=2),
        publication_delta_hours=2.0,
        source_type=source_type,
    )


def retrieval_result(
    status: SemanticRetrievalStatus,
    *,
    candidates: tuple[SemanticCandidate, ...] = (),
    embedding_version: str = VERSION,
) -> SemanticRetrievalResult:
    reasons = {
        SemanticRetrievalStatus.DISABLED: "semantic_disabled",
        SemanticRetrievalStatus.QUERY_NOT_FOUND: "source_document_not_found",
        SemanticRetrievalStatus.QUERY_NOT_READY: "query_embedding_incomplete",
    }
    return SemanticRetrievalResult(
        status=status,
        reason=reasons.get(status),
        query_source_article_id=100,
        embedding_version=embedding_version,
        requested_top_k=10,
        lookback_hours=72,
        query_document_id=(
            None
            if status in {SemanticRetrievalStatus.DISABLED, SemanticRetrievalStatus.QUERY_NOT_FOUND}
            else 10
        ),
        query_published_at=QUERY_TIME,
        query_chunks_available=4,
        query_chunks_considered=4,
        chunk_matches_per_query=50,
        candidates=candidates,
    )


class MemoryAssessmentWriter:
    def __init__(self) -> None:
        self.rows: dict[tuple[object, ...], tuple[int, SemanticAssessmentDraft, int]] = {}
        self.calls: list[SemanticAssessmentDraft] = []
        self.error: Exception | None = None
        self.next_id = 1

    def save_assessment(self, draft: SemanticAssessmentDraft) -> int:
        self.calls.append(draft)
        if self.error is not None:
            raise self.error
        key = (
            draft.query_source_article_id,
            draft.query_document_id or 0,
            draft.embedding_version,
            draft.semantic_version,
        )
        provisional = None
        if draft.query_document_id is not None:
            provisional_key = (
                draft.query_source_article_id,
                0,
                draft.embedding_version,
                draft.semantic_version,
            )
            provisional = self.rows.pop(provisional_key, None)
        existing = self.rows.get(key)
        if provisional is not None:
            assessment_id = provisional[0]
            count = provisional[2] + (existing[2] if existing else 0) + 1
        elif existing is not None:
            assessment_id = existing[0]
            count = existing[2] + 1
        else:
            assessment_id = self.next_id
            self.next_id += 1
            count = 1
        self.rows[key] = (assessment_id, draft, count)
        return assessment_id


class StubRetriever:
    def __init__(self, *results: SemanticRetrievalResult | Exception) -> None:
        self.results = list(results)
        self.calls = 0

    def retrieve_source_neighbors(self, source_article_id: int):
        self.calls += 1
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class MinimalVectorRepository:
    def get_latest_source_document(self, source_article_id: int):
        return VectorDocumentRecord(
            id=10,
            document_key="source_article:100",
            source_type=SourceType.SOURCE_ARTICLE,
            source_article_id=source_article_id,
            rich_article_id=None,
            source_url="https://source.example.test/100",
            title="Query",
            published_at=QUERY_TIME,
            content_hash="a" * 64,
            content_version="source-v1",
        )


class SemanticShadowMappingTests(unittest.TestCase):
    def run_stubbed(
        self,
        retriever: StubRetriever,
        *,
        writer: MemoryAssessmentWriter | None = None,
        settings: SemanticRetrievalSettings = SETTINGS,
        semantic_version: str = SEMANTIC_VERSION,
    ):
        writer = writer or MemoryAssessmentWriter()
        with patch.object(shadow, "SemanticRetrievalService", return_value=retriever):
            result = run_semantic_shadow(
                100,
                vector_enabled=True,
                semantic_enabled=True,
                semantic_version=semantic_version,
                settings=settings,
                vector_repository=MinimalVectorRepository(),
                assessment_repository=writer,
            )
        return result, writer

    def test_disabled_flags_are_noop_without_repository_construction(self):
        for vector_enabled, semantic_enabled, reason in (
            (False, True, "vector_disabled"),
            (True, False, "semantic_disabled"),
        ):
            with self.subTest(reason=reason), patch.object(
                shadow, "VectorRepository"
            ) as repository, patch.object(
                shadow, "SemanticAssessmentRepository"
            ) as assessment_repository:
                result = run_semantic_shadow(
                    100,
                    vector_enabled=vector_enabled,
                    semantic_enabled=semantic_enabled,
                    semantic_version=SEMANTIC_VERSION,
                    settings=SETTINGS,
                )
            self.assertEqual(result.status, SemanticAssessmentStatus.DISABLED)
            self.assertEqual(result.reason, reason)
            self.assertFalse(result.persisted)
            repository.assert_not_called()
            assessment_repository.assert_not_called()

    def test_query_not_found_maps_to_persisted_not_ready(self):
        result, writer = self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.QUERY_NOT_FOUND))
        )
        self.assertEqual(result.status, SemanticAssessmentStatus.NOT_READY)
        self.assertEqual(result.reason, "source_document_not_found")
        self.assertIsNone(writer.calls[0].query_document_id)

    def test_query_not_ready_maps_to_persisted_not_ready(self):
        result, writer = self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.QUERY_NOT_READY))
        )
        self.assertEqual(result.status, SemanticAssessmentStatus.NOT_READY)
        self.assertEqual(writer.calls[0].query_document_id, 10)

    def test_no_candidates_maps_to_persisted_no_candidates(self):
        result, writer = self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.NO_CANDIDATES))
        )
        self.assertEqual(result.status, SemanticAssessmentStatus.NO_CANDIDATES)
        self.assertEqual(writer.calls[0].evidence, ())

    def test_retrieved_maps_to_bounded_candidate_evidence(self):
        result, writer = self.run_stubbed(
            StubRetriever(
                retrieval_result(
                    SemanticRetrievalStatus.RETRIEVED,
                    candidates=(candidate(),),
                )
            )
        )
        self.assertEqual(result.status, SemanticAssessmentStatus.RETRIEVED)
        self.assertEqual(result.candidate_count, 1)
        evidence = writer.calls[0].evidence[0]
        self.assertEqual(evidence["candidate_source_article_id"], 201)
        self.assertEqual(evidence["native_distance"], 0.125)
        self.assertEqual(evidence["source_type"], "source_article")
        self.assertNotIn("chunk_text", evidence)
        self.assertNotIn("embedding", evidence)

    def test_retrieval_exception_persists_error_and_returns_fail_open(self):
        result, writer = self.run_stubbed(
            StubRetriever(RuntimeError("vector contents must not be logged"))
        )
        self.assertEqual(result.status, SemanticAssessmentStatus.ERROR)
        self.assertTrue(result.persisted)
        error = writer.calls[0]
        self.assertEqual(error.error_type, "RuntimeError")
        self.assertEqual(error.safe_error, "RuntimeError: retrieval failed")
        self.assertNotIn("contents", error.safe_error)

    def test_persistence_exception_returns_error_without_escaping(self):
        writer = MemoryAssessmentWriter()
        writer.error = RuntimeError("database credentials must not be logged")
        result, _writer = self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.NO_CANDIDATES)),
            writer=writer,
        )
        self.assertEqual(result.status, SemanticAssessmentStatus.ERROR)
        self.assertEqual(result.reason, "persistence_failed")
        self.assertFalse(result.persisted)
        self.assertEqual(result.error_type, "RuntimeError")

    def test_nonfinite_or_generated_evidence_cannot_persist_as_success(self):
        cases = (
            candidate(distance=float("nan")),
            candidate(source_type=SourceType.COINCOURIER_GENERATED),
            candidate(source_article_id=100),
        )
        for bad_candidate in cases:
            with self.subTest(candidate=bad_candidate):
                result, writer = self.run_stubbed(
                    StubRetriever(
                        retrieval_result(
                            SemanticRetrievalStatus.RETRIEVED,
                            candidates=(bad_candidate,),
                        )
                    )
                )
                self.assertEqual(result.status, SemanticAssessmentStatus.ERROR)
                self.assertEqual(writer.calls[-1].status, SemanticAssessmentStatus.ERROR)

    def test_same_identity_reconciles_to_one_assessment(self):
        writer = MemoryAssessmentWriter()
        retriever = StubRetriever(
            retrieval_result(SemanticRetrievalStatus.NO_CANDIDATES)
        )
        self.run_stubbed(retriever, writer=writer)
        self.run_stubbed(retriever, writer=writer)
        self.assertEqual(len(writer.rows), 1)
        self.assertEqual(next(iter(writer.rows.values()))[2], 2)

    def test_not_ready_reconciles_to_retrieved_for_same_document_identity(self):
        writer = MemoryAssessmentWriter()
        self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.QUERY_NOT_READY)),
            writer=writer,
        )
        self.run_stubbed(
            StubRetriever(
                retrieval_result(
                    SemanticRetrievalStatus.RETRIEVED,
                    candidates=(candidate(),),
                )
            ),
            writer=writer,
        )
        self.assertEqual(len(writer.rows), 1)
        stored = next(iter(writer.rows.values()))
        self.assertEqual(stored[1].status, SemanticAssessmentStatus.RETRIEVED)
        self.assertEqual(stored[2], 2)

    def test_query_not_found_reconciles_when_document_later_appears(self):
        writer = MemoryAssessmentWriter()
        first, _ = self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.QUERY_NOT_FOUND)),
            writer=writer,
        )
        provisional = next(iter(writer.rows.values()))

        second, _ = self.run_stubbed(
            StubRetriever(
                retrieval_result(
                    SemanticRetrievalStatus.RETRIEVED,
                    candidates=(candidate(),),
                )
            ),
            writer=writer,
        )
        resolved = next(iter(writer.rows.values()))

        self.assertEqual(first.status, SemanticAssessmentStatus.NOT_READY)
        self.assertEqual(second.status, SemanticAssessmentStatus.RETRIEVED)
        self.assertEqual(len(writer.rows), 1)
        self.assertEqual(resolved[0], provisional[0])
        self.assertEqual(resolved[1].query_document_id, 10)
        self.assertEqual(resolved[2], 2)

        third, _ = self.run_stubbed(
            StubRetriever(
                retrieval_result(
                    SemanticRetrievalStatus.RETRIEVED,
                    candidates=(candidate(),),
                )
            ),
            writer=writer,
        )
        replayed = next(iter(writer.rows.values()))
        self.assertEqual(third.assessment_id, provisional[0])
        self.assertEqual(len(writer.rows), 1)
        self.assertEqual(replayed[0], provisional[0])
        self.assertEqual(replayed[2], 3)

    def test_different_embedding_or_semantic_versions_are_separate(self):
        writer = MemoryAssessmentWriter()
        self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.NO_CANDIDATES)),
            writer=writer,
        )
        other_settings = SemanticRetrievalSettings(
            vector_enabled=True,
            semantic_enabled=True,
            embedding_version="synthetic:other:1536:chunk-v1",
        )
        self.run_stubbed(
            StubRetriever(
                retrieval_result(
                    SemanticRetrievalStatus.NO_CANDIDATES,
                    embedding_version=other_settings.embedding_version,
                )
            ),
            writer=writer,
            settings=other_settings,
        )
        self.run_stubbed(
            StubRetriever(retrieval_result(SemanticRetrievalStatus.NO_CANDIDATES)),
            writer=writer,
            semantic_version="semantic-shadow-v2",
        )
        self.assertEqual(len(writer.rows), 3)

    def test_replay_replaces_instead_of_duplicating_candidate_evidence(self):
        writer = MemoryAssessmentWriter()
        retrieved = retrieval_result(
            SemanticRetrievalStatus.RETRIEVED,
            candidates=(candidate(),),
        )
        self.run_stubbed(StubRetriever(retrieved), writer=writer)
        self.run_stubbed(StubRetriever(retrieved), writer=writer)
        stored = next(iter(writer.rows.values()))[1]
        self.assertEqual(len(stored.evidence), 1)


class ExistingVectorReadTests(unittest.TestCase):
    class ReadyRepository(MinimalVectorRepository):
        def __init__(self) -> None:
            self.enqueue_embedding_job = Mock(
                side_effect=AssertionError("semantic shadow must not enqueue")
            )

        def get_document_embedding_job(self, document_id, embedding_version):
            return EmbeddingJobRecord(
                id=1,
                document_id=document_id,
                embedding_version=embedding_version,
                status="completed",
                attempt_count=1,
                claim_token=None,
                claimed_at=None,
                last_error=None,
            )

        def get_chunks(self, document_id, *, embedding_version=None):
            return [
                VectorChunkRecord(
                    id=1,
                    document_id=document_id,
                    chunk_index=0,
                    chunk_text="Synthetic query chunk",
                    chunk_hash="a" * 64,
                    embedding=(1.0,),
                    embedding_model="synthetic",
                    embedding_dimensions=1536,
                    embedding_version=embedding_version,
                )
            ]

        def nearest_chunks(self, query_embedding, **kwargs):
            def match(source_id, document_id, source_type, distance):
                return VectorMatch(
                    distance=distance,
                    document_id=document_id,
                    document_key=f"{source_type.value}:{document_id}",
                    source_type=source_type,
                    source_article_id=source_id,
                    rich_article_id=(document_id if source_type is not SourceType.SOURCE_ARTICLE else None),
                    source_url=f"https://source.example.test/{source_id}",
                    title=f"Candidate {source_id}",
                    published_at=QUERY_TIME - timedelta(hours=1),
                    chunk_id=document_id * 10,
                    chunk_index=0,
                    chunk_text="Synthetic candidate chunk",
                    chunk_hash="b" * 64,
                    embedding_model="synthetic",
                    embedding_version=kwargs["embedding_version"],
                )

            return [
                match(100, 9, SourceType.SOURCE_ARTICLE, 0.01),
                match(201, 20, SourceType.COINCOURIER_GENERATED, 0.02),
                match(202, 21, SourceType.SOURCE_ARTICLE, 0.03),
            ]

    def test_embedding_disabled_does_not_disable_existing_vector_reads(self):
        with (
            patch.object(config, "VECTOR_ENABLED", True),
            patch.object(config, "SEMANTIC_SHADOW_ENABLED", True),
            patch.object(config, "EMBEDDING_ENABLED", False),
        ):
            settings = SemanticRetrievalSettings.from_config()
        self.assertTrue(settings.vector_enabled)
        self.assertTrue(settings.semantic_enabled)

        repository = self.ReadyRepository()
        writer = MemoryAssessmentWriter()
        with patch("embeddings.provider.OpenAIEmbeddingProvider") as provider:
            result = run_semantic_shadow(
                100,
                vector_enabled=True,
                semantic_enabled=True,
                semantic_version=SEMANTIC_VERSION,
                settings=settings,
                vector_repository=repository,
                assessment_repository=writer,
            )
        self.assertEqual(result.status, SemanticAssessmentStatus.RETRIEVED)
        self.assertEqual(
            writer.calls[0].evidence[0]["candidate_source_article_id"],
            202,
        )
        provider.assert_not_called()
        repository.enqueue_embedding_job.assert_not_called()


class SemanticAssessmentRepositoryTests(unittest.TestCase):
    @staticmethod
    def valid_evidence(candidate_id: int = 201):
        return {
            "candidate_source_article_id": candidate_id,
            "candidate_document_id": candidate_id * 10,
            "candidate_document_key": f"source_article:{candidate_id}",
            "candidate_source_url": f"https://source.example.test/{candidate_id}",
            "candidate_title": f"Candidate {candidate_id}",
            "native_distance": 0.1,
            "embedding_version": VERSION,
            "best_query_chunk_index": 0,
            "best_candidate_chunk_index": 0,
            "matched_query_chunk_count": 1,
            "published_at": "2026-09-01T11:00:00Z",
            "publication_delta_hours": 1.0,
            "source_type": "source_article",
        }

    def draft(self, **changes):
        values = {
            "query_source_article_id": 100,
            "query_document_id": 10,
            "embedding_version": VERSION,
            "semantic_version": SEMANTIC_VERSION,
            "status": SemanticAssessmentStatus.RETRIEVED,
            "reason": None,
            "lookback_hours": 72,
            "requested_top_k": 10,
            "query_chunks_available": 2,
            "query_chunks_considered": 2,
            "best_native_distance": 0.1,
            "evidence": (self.valid_evidence(),),
        }
        values.update(changes)
        return SemanticAssessmentDraft(**values)

    def test_repository_uses_idempotent_short_transaction(self):
        cursor = Mock()
        cursor.lastrowid = 7
        cursor.fetchall.return_value = []
        connection = Mock()
        connection.cursor.return_value = cursor
        repository = SemanticAssessmentRepository(connect=lambda: connection)
        self.assertEqual(repository.save_assessment(self.draft()), 7)
        sql = cursor.execute.call_args.args[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("evaluation_count = evaluation_count + 1", sql)
        connection.start_transaction.assert_called_once_with()
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_repository_promotes_locked_provisional_row(self):
        created_at = datetime(2026, 9, 1, 12, 0, 0)
        cursor = Mock()
        cursor.fetchall.return_value = [
            {
                "id": 5,
                "query_document_id": None,
                "evaluation_count": 1,
                "created_at": created_at,
            }
        ]
        connection = Mock()
        connection.cursor.return_value = cursor
        repository = SemanticAssessmentRepository(connect=lambda: connection)

        self.assertEqual(repository.save_assessment(self.draft()), 5)

        calls = cursor.execute.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("FOR UPDATE", calls[0].args[0])
        self.assertIn("UPDATE semantic_shadow_assessments", calls[1].args[0])
        self.assertEqual(calls[1].args[1][-3:], (2, created_at, 5))
        connection.commit.assert_called_once_with()

    def test_repository_merges_existing_resolved_row_during_promotion(self):
        first_created = datetime(2026, 9, 1, 12, 0, 0)
        cursor = Mock()
        cursor.fetchall.return_value = [
            {
                "id": 5,
                "query_document_id": None,
                "evaluation_count": 1,
                "created_at": first_created,
            },
            {
                "id": 7,
                "query_document_id": 10,
                "evaluation_count": 2,
                "created_at": datetime(2026, 9, 1, 12, 5, 0),
            },
        ]
        connection = Mock()
        connection.cursor.return_value = cursor
        repository = SemanticAssessmentRepository(connect=lambda: connection)

        self.assertEqual(repository.save_assessment(self.draft()), 5)

        calls = cursor.execute.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn("DELETE FROM semantic_shadow_assessments", calls[1].args[0])
        self.assertEqual(calls[1].args[1], (7,))
        self.assertEqual(calls[2].args[1][-3:], (4, first_created, 5))
        connection.commit.assert_called_once_with()

    def test_repository_rolls_back_failed_write(self):
        cursor = Mock()
        cursor.execute.side_effect = RuntimeError("write unavailable")
        connection = Mock()
        connection.cursor.return_value = cursor
        repository = SemanticAssessmentRepository(connect=lambda: connection)
        with self.assertRaisesRegex(RuntimeError, "write unavailable"):
            repository.save_assessment(self.draft())
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_storage_model_rejects_unbounded_or_sensitive_evidence(self):
        bad = self.valid_evidence()
        bad["chunk_text"] = "must not persist"
        with self.assertRaisesRegex(ValueError, "bodies, chunks, or vectors"):
            self.draft(evidence=(bad,))
        with self.assertRaisesRegex(ValueError, "top-K"):
            self.draft(
                requested_top_k=1,
                evidence=(self.valid_evidence(201), self.valid_evidence(202)),
            )

    def test_migration_has_null_safe_identity_fk_indexes_and_json_check(self):
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("query_document_identity", sql)
        self.assertIn("uq_semantic_shadow_identity", sql)
        self.assertIn("fk_semantic_shadow_query_document", sql)
        self.assertIn("idx_semantic_shadow_source_version", sql)
        self.assertIn("idx_semantic_shadow_document_version", sql)
        self.assertIn("JSON_VALID(evidence_json)", sql)
        self.assertIn("JSON_LENGTH(evidence_json) = candidate_count", sql)
        self.assertNotIn("is_duplicate", sql.lower())


class SemanticPipelineFailOpenTests(unittest.TestCase):
    def run_process(self, semantic_side_effect):
        raw = {
            "id": 100,
            "news_url": "https://source.example.test/100",
            "title": "Source title",
            "source_name": "Source",
            "chosen_for_publish": 1,
            "scheduled_for": "2026-09-01 12:30:00",
            "processed": 0,
            "published": 0,
        }
        before = deepcopy(raw)
        final_doc = {
            "title": "Processed title",
            "full_text": "<p>Processed body.</p>",
            "seo_focus": "focus",
            "seo_slug": "processed-title",
            "seo_meta": "Processed article metadata.",
        }
        with (
            patch.object(gpt_processor, "DUPLICATE_SHADOW_ENABLED", False),
            patch.object(gpt_processor, "VECTOR_ENABLED", True),
            patch.object(gpt_processor, "SEMANTIC_SHADOW_ENABLED", True),
            patch.object(
                gpt_processor,
                "run_semantic_shadow",
                side_effect=semantic_side_effect,
            ) as semantic_runner,
            patch.object(gpt_processor, "enrich_with_search", return_value={}) as enrich,
            patch.object(gpt_processor, "_maybe_video_url", return_value=""),
            patch.object(gpt_processor, "classify_and_rewrite", return_value=({}, "test")),
            patch.object(gpt_processor, "repair_if_needed", return_value=final_doc),
            patch.object(gpt_processor, "build_news_schema_jsonld", return_value=None),
            patch.object(gpt_processor, "validate_rewritten_article"),
            patch.object(gpt_processor, "store_rich_news") as store,
            patch.object(gpt_processor, "mark_processed") as complete,
            patch.object(gpt_processor.logging, "info"),
            patch.object(gpt_processor.logging, "error"),
            patch.object(gpt_processor.logging, "exception"),
        ):
            succeeded = gpt_processor.process_one(raw)
        self.assertTrue(succeeded)
        self.assertEqual(raw, before)
        semantic_runner.assert_called_once_with(
            100,
            vector_enabled=True,
            semantic_enabled=True,
        )
        enrich.assert_called_once_with(raw)
        store.assert_called_once_with(final_doc, raw)
        complete.assert_called_once_with(raw["news_url"])

    def shadow_result(self, status: SemanticAssessmentStatus):
        return shadow.SemanticShadowRunResult(
            status=status,
            reason=None,
            query_source_article_id=100,
            query_document_id=10,
            embedding_version=VERSION,
            semantic_version=SEMANTIC_VERSION,
            persisted=status is not SemanticAssessmentStatus.ERROR,
        )

    def test_not_ready_no_candidates_and_retrieved_all_continue_processing(self):
        for status in (
            SemanticAssessmentStatus.NOT_READY,
            SemanticAssessmentStatus.NO_CANDIDATES,
            SemanticAssessmentStatus.RETRIEVED,
        ):
            with self.subTest(status=status):
                self.run_process(self.shadow_result(status))

    def test_vector_database_exception_continues_processing(self):
        self.run_process(RuntimeError("vector unavailable"))

    def test_semantic_persistence_failure_continues_processing(self):
        self.run_process(self.shadow_result(SemanticAssessmentStatus.ERROR))

    def test_disabled_semantic_flags_do_not_call_runner(self):
        for vector_enabled, semantic_enabled in ((False, True), (True, False)):
            with self.subTest(
                vector_enabled=vector_enabled,
                semantic_enabled=semantic_enabled,
            ), patch.object(
                gpt_processor, "VECTOR_ENABLED", vector_enabled
            ), patch.object(
                gpt_processor, "SEMANTIC_SHADOW_ENABLED", semantic_enabled
            ), patch.object(gpt_processor, "run_semantic_shadow") as runner:
                gpt_processor._run_semantic_shadow_fail_open({"id": 100})
            runner.assert_not_called()

    def test_semantic_runs_after_phase5_and_before_enrichment(self):
        events = []
        with (
            patch.object(
                gpt_processor,
                "_run_duplicate_shadow_fail_open",
                side_effect=lambda _raw: events.append("deterministic"),
            ),
            patch.object(
                gpt_processor,
                "_run_semantic_shadow_fail_open",
                side_effect=lambda _raw: events.append("semantic"),
            ),
            patch.object(
                gpt_processor,
                "enrich_with_search",
                side_effect=lambda _raw: events.append("enrichment") or {},
            ),
            patch.object(gpt_processor, "_maybe_video_url", return_value=""),
            patch.object(gpt_processor, "classify_and_rewrite", side_effect=RuntimeError),
            patch.object(gpt_processor.logging, "exception"),
        ):
            gpt_processor.process_one({"id": 100, "title": "T", "source_name": "S"})
        self.assertEqual(events, ["deterministic", "semantic", "enrichment"])

    def test_durable_claim_transaction_finishes_before_process_one(self):
        source = inspect.getsource(gpt_processor._process_news_with_durable_claims)
        self.assertLess(source.index("repository.claim_next"), source.index("process_one"))
        self.assertIn("after the claim transaction has been committed", source)


if __name__ == "__main__":
    unittest.main()
