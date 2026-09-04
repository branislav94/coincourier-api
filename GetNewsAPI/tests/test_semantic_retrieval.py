from __future__ import annotations

import inspect
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "semantic_relationships.json"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config
from semantic_retrieval.evaluation import (
    EvaluationFixture,
    LabeledRelationship,
    RelationshipLabel,
    RelevanceDefinition,
    evaluate_retrieval,
    load_evaluation_fixture,
)
from semantic_retrieval.models import (
    MAX_QUERY_CHUNKS,
    MAX_SEMANTIC_TOP_K,
    SemanticCandidate,
    SemanticRetrievalResult,
    SemanticRetrievalSettings,
    SemanticRetrievalStatus,
)
from semantic_retrieval.service import SemanticRetrievalService
from vector_store.models import (
    EmbeddingJobRecord,
    SourceType,
    VECTOR_DIMENSIONS,
    VectorChunkRecord,
    VectorDocumentRecord,
    VectorMatch,
)


VERSION = "openai:text-embedding-3-small:1536:chunk-v1"
QUERY_TIME = datetime(2026, 9, 1, 12, 0, 0)


def fake_vector(marker: float) -> tuple[float, ...]:
    return (marker, *([0.0] * (VECTOR_DIMENSIONS - 1)))


def document(
    document_id: int = 10,
    source_article_id: int = 100,
    *,
    source_type: SourceType = SourceType.SOURCE_ARTICLE,
    rich_article_id: int | None = None,
    published_at: datetime | None = QUERY_TIME,
) -> VectorDocumentRecord:
    return VectorDocumentRecord(
        id=document_id,
        document_key=(
            f"source_article:{source_article_id}"
            if source_type is SourceType.SOURCE_ARTICLE
            else f"coincourier_generated:{rich_article_id}"
        ),
        source_type=source_type,
        source_article_id=source_article_id,
        rich_article_id=rich_article_id,
        source_url=f"https://source.example.test/{source_article_id}",
        title=f"Article {source_article_id}",
        published_at=published_at,
        content_hash="a" * 64,
        content_version="chunk-v1:" + "a" * 64,
    )


def chunk(
    document_id: int = 10,
    index: int = 0,
    *,
    version: str = VERSION,
) -> VectorChunkRecord:
    return VectorChunkRecord(
        id=document_id * 100 + index,
        document_id=document_id,
        chunk_index=index,
        chunk_text=f"Synthetic chunk {index}",
        chunk_hash=f"{index:064x}",
        embedding=fake_vector(float(index + 1)),
        embedding_model="text-embedding-3-small",
        embedding_dimensions=VECTOR_DIMENSIONS,
        embedding_version=version,
    )


def completed_job(document_id: int = 10, *, status: str = "completed") -> EmbeddingJobRecord:
    return EmbeddingJobRecord(
        id=document_id * 10,
        document_id=document_id,
        embedding_version=VERSION,
        status=status,
        attempt_count=1,
        claim_token=None,
        claimed_at=None,
        last_error=None,
    )


def match(
    source_article_id: int,
    distance: float,
    *,
    document_id: int | None = None,
    chunk_index: int = 0,
    source_type: SourceType = SourceType.SOURCE_ARTICLE,
    rich_article_id: int | None = None,
    published_at: datetime | None = None,
    version: str = VERSION,
) -> VectorMatch:
    candidate_document_id = document_id or source_article_id * 10
    candidate_time = published_at if published_at is not None else QUERY_TIME - timedelta(hours=1)
    return VectorMatch(
        distance=distance,
        document_id=candidate_document_id,
        document_key=(
            f"source_article:{source_article_id}"
            if source_type is SourceType.SOURCE_ARTICLE
            else f"coincourier_generated:{rich_article_id}"
        ),
        source_type=source_type,
        source_article_id=source_article_id,
        rich_article_id=rich_article_id,
        source_url=f"https://candidate.example.test/{source_article_id}",
        title=f"Candidate {source_article_id}",
        published_at=candidate_time,
        chunk_id=candidate_document_id * 100 + chunk_index,
        chunk_index=chunk_index,
        chunk_text="Synthetic candidate chunk",
        chunk_hash="b" * 64,
        embedding_model="text-embedding-3-small",
        embedding_version=version,
    )


class FakeVectorRepository:
    def __init__(self) -> None:
        self.document = document()
        self.job = completed_job()
        self.chunks = [chunk()]
        self.matches_by_marker: dict[float, list[VectorMatch]] = {}
        self.nearest_calls = []
        self.raise_on_nearest: Exception | None = None

    def get_latest_source_document(self, source_article_id):
        return self.document

    def get_document_embedding_job(self, document_id, embedding_version):
        return self.job

    def get_chunks(self, document_id, *, embedding_version=None):
        return list(self.chunks)

    def nearest_chunks(self, query_embedding, **kwargs):
        self.nearest_calls.append((query_embedding, kwargs))
        if self.raise_on_nearest is not None:
            raise self.raise_on_nearest
        return list(self.matches_by_marker.get(float(query_embedding[0]), []))


def service(
    repository: FakeVectorRepository,
    *,
    vector_enabled: bool = True,
    semantic_enabled: bool = True,
    lookback_hours: int = 72,
    top_k: int = 10,
) -> SemanticRetrievalService:
    return SemanticRetrievalService(
        repository=repository,
        settings=SemanticRetrievalSettings(
            vector_enabled=vector_enabled,
            semantic_enabled=semantic_enabled,
            embedding_version=VERSION,
            lookback_hours=lookback_hours,
            top_k=top_k,
        ),
    )


class SemanticRetrievalTests(unittest.TestCase):
    def test_query_article_retrieves_nearest_historical_article(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [match(201, 0.04)]
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(result.status, SemanticRetrievalStatus.RETRIEVED)
        self.assertEqual([item.candidate_source_article_id for item in result.candidates], [201])

    def test_same_document_and_source_identity_are_excluded(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [
            match(100, 0.0, document_id=10),
            match(100, 0.01, document_id=9),
            match(201, 0.02),
        ]
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual([item.candidate_source_article_id for item in result.candidates], [201])
        kwargs = repository.nearest_calls[0][1]
        self.assertEqual(kwargs["exclude_document_id"], 10)
        self.assertEqual(kwargs["exclude_source_article_id"], 100)

    def test_generated_candidate_is_never_duplicate_evidence(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [
            match(
                201,
                0.0,
                document_id=2001,
                source_type=SourceType.COINCOURIER_GENERATED,
                rich_article_id=301,
            ),
            match(202, 0.1),
        ]
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual([item.candidate_source_article_id for item in result.candidates], [202])
        self.assertEqual(
            repository.nearest_calls[0][1]["source_type"],
            SourceType.SOURCE_ARTICLE,
        )

    def test_incompatible_embedding_version_is_excluded(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [match(201, 0.01, version="other:v2")]
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(result.status, SemanticRetrievalStatus.NO_CANDIDATES)

    def test_future_outside_lookback_and_null_dated_candidates_are_excluded(self):
        repository = FakeVectorRepository()
        null_dated = match(203, 0.03)
        null_dated = VectorMatch(**{**null_dated.__dict__, "published_at": None})
        repository.matches_by_marker[1.0] = [
            match(201, 0.01, published_at=QUERY_TIME + timedelta(seconds=1)),
            match(202, 0.02, published_at=QUERY_TIME - timedelta(hours=73)),
            null_dated,
            match(204, 0.04, published_at=QUERY_TIME - timedelta(hours=72)),
        ]
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual([item.candidate_source_article_id for item in result.candidates], [204])
        kwargs = repository.nearest_calls[0][1]
        self.assertEqual(kwargs["published_after"], QUERY_TIME - timedelta(hours=72))
        self.assertEqual(kwargs["published_before"], QUERY_TIME)

    def test_top_k_is_distinct_by_source_article(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [
            match(201, 0.01, document_id=2010, chunk_index=0),
            match(201, 0.02, document_id=2010, chunk_index=1),
            match(201, 0.03, document_id=2011, chunk_index=0),
            match(202, 0.04),
            match(203, 0.05),
        ]
        result = service(repository).retrieve_source_neighbors(100, top_k=2)
        self.assertEqual(
            [item.candidate_source_article_id for item in result.candidates],
            [201, 202],
        )

    def test_minimum_chunk_distance_drives_rank_and_preserves_evidence(self):
        repository = FakeVectorRepository()
        repository.chunks = [chunk(index=0), chunk(index=1)]
        repository.matches_by_marker = {
            1.0: [match(201, 0.30, chunk_index=4), match(202, 0.20, chunk_index=2)],
            2.0: [match(201, 0.10, chunk_index=7)],
        }
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(
            [item.candidate_source_article_id for item in result.candidates],
            [201, 202],
        )
        winner = result.candidates[0]
        self.assertEqual(winner.native_distance, 0.10)
        self.assertEqual(winner.best_query_chunk_index, 1)
        self.assertEqual(winner.best_candidate_chunk_index, 7)
        self.assertEqual(winner.matched_query_chunk_count, 2)
        self.assertEqual(winner.candidate_title, "Candidate 201")
        self.assertEqual(winner.source_type, SourceType.SOURCE_ARTICLE)

    def test_distance_and_tie_ordering_are_deterministic(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [
            match(203, 0.20),
            match(202, 0.10),
            match(201, 0.10),
        ]
        first = service(repository).retrieve_source_neighbors(100)
        second = service(repository).retrieve_source_neighbors(100)
        expected = [201, 202, 203]
        self.assertEqual([item.candidate_source_article_id for item in first.candidates], expected)
        self.assertEqual(first, second)

    def test_missing_or_incomplete_query_embeddings_are_not_ready(self):
        for job, chunks, reason in (
            (completed_job(status="pending"), [chunk()], "query_embedding_incomplete"),
            (completed_job(), [], "query_vectors_missing"),
        ):
            with self.subTest(reason=reason):
                repository = FakeVectorRepository()
                repository.job = job
                repository.chunks = chunks
                result = service(repository).retrieve_source_neighbors(100)
                self.assertEqual(result.status, SemanticRetrievalStatus.QUERY_NOT_READY)
                self.assertEqual(result.reason, reason)
                self.assertEqual(repository.nearest_calls, [])

    def test_missing_query_document_and_null_query_date_are_explicit(self):
        repository = FakeVectorRepository()
        repository.document = None
        missing = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(missing.status, SemanticRetrievalStatus.QUERY_NOT_FOUND)
        repository.document = document(published_at=None)
        undated = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(undated.status, SemanticRetrievalStatus.QUERY_NOT_READY)
        self.assertEqual(undated.reason, "query_publication_time_missing")

    def test_query_document_must_be_a_source_article(self):
        repository = FakeVectorRepository()
        repository.document = document(
            source_type=SourceType.COINCOURIER_GENERATED,
            rich_article_id=501,
        )
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(result.status, SemanticRetrievalStatus.QUERY_NOT_READY)
        self.assertEqual(result.reason, "query_document_is_not_source_article")

    def test_no_candidates_is_distinct_from_infrastructure_failure(self):
        repository = FakeVectorRepository()
        empty = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(empty.status, SemanticRetrievalStatus.NO_CANDIDATES)
        repository.raise_on_nearest = RuntimeError("vector database unavailable")
        with self.assertRaisesRegex(RuntimeError, "vector database unavailable"):
            service(repository).retrieve_source_neighbors(100)

    def test_disabled_flags_open_no_retrieval_work(self):
        for vector_enabled, semantic_enabled, reason in (
            (False, True, "vector_disabled"),
            (True, False, "semantic_disabled"),
        ):
            with self.subTest(reason=reason):
                repository = FakeVectorRepository()
                result = service(
                    repository,
                    vector_enabled=vector_enabled,
                    semantic_enabled=semantic_enabled,
                ).retrieve_source_neighbors(100)
                self.assertEqual(result.status, SemanticRetrievalStatus.DISABLED)
                self.assertEqual(result.reason, reason)
                self.assertEqual(repository.nearest_calls, [])

    def test_query_chunk_and_candidate_work_are_hard_bounded(self):
        repository = FakeVectorRepository()
        repository.chunks = [chunk(index=index) for index in range(20)]
        result = service(repository).retrieve_source_neighbors(100)
        self.assertEqual(result.query_chunks_available, 20)
        self.assertEqual(result.query_chunks_considered, MAX_QUERY_CHUNKS)
        self.assertEqual(len(repository.nearest_calls), MAX_QUERY_CHUNKS)
        self.assertTrue(all(call[1]["top_k"] == 50 for call in repository.nearest_calls))
        considered_markers = [call[0][0] for call in repository.nearest_calls]
        self.assertEqual(considered_markers[0], 1.0)
        self.assertEqual(considered_markers[-1], 20.0)
        self.assertEqual(len(set(considered_markers)), MAX_QUERY_CHUNKS)
        repository.nearest_calls.clear()
        service(repository).retrieve_source_neighbors(100)
        self.assertEqual(
            [call[0][0] for call in repository.nearest_calls],
            considered_markers,
        )
        with self.assertRaises(ValueError):
            service(repository).retrieve_source_neighbors(100, top_k=MAX_SEMANTIC_TOP_K + 1)

    def test_nonfinite_native_distance_propagates_as_data_error(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [match(201, float("nan"))]
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            service(repository).retrieve_source_neighbors(100)

    def test_native_distance_is_exposed_without_derived_similarity(self):
        repository = FakeVectorRepository()
        repository.matches_by_marker[1.0] = [match(201, 0.125)]
        candidate = service(repository).retrieve_source_neighbors(100).candidates[0]
        self.assertEqual(candidate.native_distance, 0.125)
        self.assertFalse(hasattr(candidate, "similarity"))


def semantic_candidate(query_id: int, candidate_id: int, distance: float) -> SemanticCandidate:
    return SemanticCandidate(
        query_document_id=query_id * 10,
        query_source_article_id=query_id,
        candidate_document_id=candidate_id * 10,
        candidate_source_article_id=candidate_id,
        candidate_document_key=f"source_article:{candidate_id}",
        candidate_source_url=f"https://candidate.example.test/{candidate_id}",
        candidate_title=f"Candidate {candidate_id}",
        native_distance=distance,
        embedding_version=VERSION,
        best_query_chunk_index=0,
        best_candidate_chunk_index=0,
        matched_query_chunk_count=1,
        published_at=QUERY_TIME - timedelta(hours=1),
        publication_delta_hours=1.0,
        source_type=SourceType.SOURCE_ARTICLE,
    )


class FakeSemanticRetriever:
    def __init__(self, candidates_by_query=None, status_by_query=None):
        self.candidates_by_query = candidates_by_query or {}
        self.status_by_query = status_by_query or {}
        self.calls = []

    def retrieve_source_neighbors(self, source_article_id, *, top_k=None):
        self.calls.append((source_article_id, top_k))
        status = self.status_by_query.get(
            source_article_id,
            SemanticRetrievalStatus.RETRIEVED,
        )
        return SemanticRetrievalResult(
            status=status,
            reason=None,
            query_source_article_id=source_article_id,
            embedding_version=VERSION,
            requested_top_k=int(top_k or 10),
            lookback_hours=72,
            query_document_id=source_article_id * 10,
            query_published_at=QUERY_TIME,
            candidates=tuple(self.candidates_by_query.get(source_article_id, ())),
        )


class SemanticEvaluationTests(unittest.TestCase):
    def test_labeled_json_fixture_loads_deterministically_with_all_labels(self):
        first = load_evaluation_fixture(FIXTURE_PATH)
        second = load_evaluation_fixture(FIXTURE_PATH)
        self.assertEqual(first, second)
        self.assertEqual(first.query_source_article_ids, (1001, 1002))
        self.assertEqual(
            {item.label for item in first.relationships},
            set(RelationshipLabel),
        )

    def test_fixture_loader_rejects_duplicate_relationship_pairs(self):
        payload = {
            "schema_version": "semantic-eval-v1",
            "relationships": [
                {
                    "query_source_article_id": 1,
                    "candidate_source_article_id": 2,
                    "label": "exact_duplicate",
                },
                {
                    "query_source_article_id": 1,
                    "candidate_source_article_id": 2,
                    "label": "related_event",
                },
            ],
        }
        with patch.object(Path, "read_text", return_value=json.dumps(payload)):
            with self.assertRaisesRegex(ValueError, "pairs must be unique"):
                load_evaluation_fixture("duplicate-pairs.json")

    def test_recall_at_k_and_first_relevant_mrr(self):
        fixture = load_evaluation_fixture(FIXTURE_PATH)
        retriever = FakeSemanticRetriever(
            {
                1001: [
                    semantic_candidate(1001, 2003, 0.05),
                    semantic_candidate(1001, 2001, 0.10),
                ],
                1002: [
                    semantic_candidate(1002, 2005, 0.06),
                    semantic_candidate(1002, 2006, 0.08),
                    semantic_candidate(1002, 2004, 0.12),
                ],
            }
        )
        metrics = evaluate_retrieval(
            fixture,
            retriever,
            top_k=3,
            relevance_definition=RelevanceDefinition.STRICT_DUPLICATE,
        )
        self.assertEqual(metrics.recall_at_k, 1.0)
        self.assertAlmostEqual(metrics.mean_reciprocal_rank, (1 / 2 + 1 / 3) / 2)
        ranks = {
            (item.query_source_article_id, item.candidate_source_article_id): item.candidate_rank
            for item in metrics.relationships
        }
        self.assertEqual(ranks[(1001, 2001)], 2)
        self.assertEqual(ranks[(1002, 2004)], 3)

    def test_strict_and_broader_relevance_definitions_remain_explicit(self):
        fixture = EvaluationFixture(
            schema_version="semantic-eval-v1",
            relationships=(
                LabeledRelationship(1, 2, RelationshipLabel.MATERIAL_UPDATE),
            ),
        )
        retriever = FakeSemanticRetriever({1: [semantic_candidate(1, 2, 0.1)]})
        strict = evaluate_retrieval(
            fixture,
            retriever,
            top_k=1,
            relevance_definition=RelevanceDefinition.STRICT_DUPLICATE,
        )
        broader = evaluate_retrieval(
            fixture,
            retriever,
            top_k=1,
            relevance_definition=RelevanceDefinition.BROADER_SAME_EVENT,
        )
        self.assertEqual(strict.evaluable_query_count, 0)
        self.assertEqual(broader.evaluable_query_count, 1)
        self.assertEqual(broader.recall_at_k, 1.0)
        self.assertNotIn(RelationshipLabel.MATERIAL_UPDATE, strict.relevant_labels)
        self.assertIn(RelationshipLabel.MATERIAL_UPDATE, broader.relevant_labels)

    def test_label_distance_distributions_and_missing_pairs_are_reported(self):
        fixture = load_evaluation_fixture(FIXTURE_PATH)
        retriever = FakeSemanticRetriever(
            {1001: [semantic_candidate(1001, 2001, 0.25)]},
            {1002: SemanticRetrievalStatus.QUERY_NOT_READY},
        )
        metrics = evaluate_retrieval(
            fixture,
            retriever,
            top_k=2,
            relevance_definition=RelevanceDefinition.STRICT_DUPLICATE,
        )
        exact = next(
            item
            for item in metrics.label_distributions
            if item.label is RelationshipLabel.EXACT_DUPLICATE
        )
        self.assertEqual(exact.retrieved_count, 1)
        self.assertEqual(exact.mean_distance, 0.25)
        self.assertEqual(len(metrics.missing_labeled_pairs), 5)
        self.assertEqual(metrics.unavailable_queries, (1002,))
        self.assertEqual(metrics.evaluable_labeled_pair_count, 3)
        self.assertAlmostEqual(metrics.top_k_labeled_coverage, 1 / 3)

    def test_unavailable_query_is_reported_but_excluded_from_quality_metrics(self):
        fixture = EvaluationFixture(
            schema_version="semantic-eval-v1",
            relationships=(
                LabeledRelationship(1, 11, RelationshipLabel.EXACT_DUPLICATE),
                LabeledRelationship(2, 22, RelationshipLabel.SAME_EVENT_DUPLICATE),
            ),
        )
        retriever = FakeSemanticRetriever(
            {1: [semantic_candidate(1, 11, 0.1)]},
            {2: SemanticRetrievalStatus.QUERY_NOT_READY},
        )
        metrics = evaluate_retrieval(
            fixture,
            retriever,
            top_k=1,
            relevance_definition=RelevanceDefinition.STRICT_DUPLICATE,
        )
        self.assertEqual(metrics.unavailable_queries, (2,))
        self.assertEqual(metrics.query_count, 2)
        self.assertEqual(metrics.evaluable_query_count, 1)
        self.assertEqual(metrics.recall_at_k, 1.0)
        self.assertEqual(metrics.mean_reciprocal_rank, 1.0)

    def test_no_candidates_is_evaluable_and_counts_as_a_retrieval_miss(self):
        fixture = EvaluationFixture(
            schema_version="semantic-eval-v1",
            relationships=(
                LabeledRelationship(1, 11, RelationshipLabel.EXACT_DUPLICATE),
            ),
        )
        retriever = FakeSemanticRetriever(
            status_by_query={1: SemanticRetrievalStatus.NO_CANDIDATES}
        )
        metrics = evaluate_retrieval(
            fixture,
            retriever,
            top_k=1,
            relevance_definition=RelevanceDefinition.STRICT_DUPLICATE,
        )
        self.assertEqual(metrics.unavailable_queries, ())
        self.assertEqual(metrics.evaluable_query_count, 1)
        self.assertEqual(metrics.recall_at_k, 0.0)
        self.assertEqual(metrics.mean_reciprocal_rank, 0.0)
        self.assertEqual(len(metrics.missing_labeled_pairs), 1)

    def test_evaluation_is_read_only_and_makes_no_provider_calls(self):
        fixture = load_evaluation_fixture(FIXTURE_PATH)
        retriever = FakeSemanticRetriever()
        with patch("embeddings.provider.OpenAIEmbeddingProvider") as provider:
            evaluate_retrieval(
                fixture,
                retriever,
                top_k=3,
                relevance_definition=RelevanceDefinition.STRICT_DUPLICATE,
            )
        provider.assert_not_called()
        self.assertEqual(retriever.calls, [(1001, 3), (1002, 3)])


class SemanticIsolationTests(unittest.TestCase):
    def test_source_defaults_are_disabled_bounded_and_threshold_free(self):
        source = inspect.getsource(config)
        self.assertIn('_env_bool("SEMANTIC_SHADOW_ENABLED", False)', source)
        self.assertIn('os.getenv("SEMANTIC_TOP_K", "10")', source)
        self.assertIn("str(DUPLICATE_LOOKBACK_HOURS)", source)
        env_example = (REPOSITORY_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertIn("SEMANTIC_SHADOW_ENABLED=false", env_example)
        self.assertIn("SEMANTIC_LOOKBACK_HOURS=72", env_example)
        self.assertIn("SEMANTIC_TOP_K=10", env_example)
        self.assertNotIn("SEMANTIC_THRESHOLD", source + env_example)

    def test_no_pipeline_or_phase5_semantic_import_exists(self):
        for relative_path in (
            "GetNewsAPI/gpt_processor.py",
            "GetNewsAPI/fetcher.py",
            "GetNewsAPI/scheduler.py",
            "GetNewsAPI/publish_to_wp.py",
            "GetNewsAPI/duplicate_detection/policy.py",
            "GetNewsAPI/duplicate_detection/shadow.py",
        ):
            source = (REPOSITORY_DIR / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("semantic_retrieval", source, relative_path)


if __name__ == "__main__":
    unittest.main()
