from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
FIXTURE_PATH = PROJECT_DIR / "tests" / "fixtures" / "semantic_calibration_labels.json"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from duplicate_detection.policy import AssessmentType
from semantic_calibration import (
    CalibrationSelection,
    DeterministicEvidence,
    InMemoryDeterministicEvidenceReader,
    InMemorySemanticEvidenceReader,
    SemanticCandidateEvidence,
    SemanticEvidenceSnapshot,
    calibrate,
    load_reviewed_labels,
    render_report_json,
)
from semantic_calibration.models import (
    LABEL_SCHEMA_VERSION,
    ReviewedLabel,
    ReviewedLabelSet,
)
from semantic_calibration.readers import (
    MariaDBDeterministicEvidenceReader,
    MariaDBSemanticEvidenceReader,
)
from semantic_retrieval.evaluation import RelationshipLabel, RelevanceDefinition
from vector_store.semantic_assessments import SemanticAssessmentStatus


POLICY_VERSION = "v1"
EMBEDDING_VERSION = "openai:text-embedding-3-small:1536:chunk-v1"
SEMANTIC_VERSION = "semantic-shadow-v1"
SELECTION = CalibrationSelection(
    deterministic_policy_version=POLICY_VERSION,
    embedding_version=EMBEDDING_VERSION,
    semantic_version=SEMANTIC_VERSION,
)


def deterministic(
    query_id: int,
    candidate_id: int,
    classification: AssessmentType,
) -> DeterministicEvidence:
    return DeterministicEvidence(
        query_source_article_id=query_id,
        candidate_source_article_id=candidate_id,
        classification=classification,
        same_provider_article_id=classification is AssessmentType.EXACT_DUPLICATE,
        same_event_id=classification
        in {AssessmentType.SAME_EVENT_DUPLICATE, AssessmentType.MATERIAL_UPDATE},
        same_canonical_url=False,
        same_content_hash=False,
        title_token_jaccard=0.75,
        publication_distance_hours=1.5,
        shared_entities=("org:Example",),
        shared_dates=(),
        shared_numbers=(),
        reason_codes=("synthetic_fixture",),
        policy_version=POLICY_VERSION,
    )


def semantic_candidate(
    candidate_id: int,
    rank: int,
    distance: float,
) -> SemanticCandidateEvidence:
    return SemanticCandidateEvidence(
        candidate_source_article_id=candidate_id,
        candidate_document_id=candidate_id * 10,
        rank=rank,
        native_distance=distance,
        best_query_chunk_index=0,
        best_candidate_chunk_index=rank,
        matched_query_chunk_count=2,
        publication_delta_hours=1.0,
    )


def semantic_snapshot(
    query_id: int,
    status: SemanticAssessmentStatus,
    candidates: tuple[SemanticCandidateEvidence, ...] = (),
    *,
    embedding_version: str = EMBEDDING_VERSION,
    semantic_version: str = SEMANTIC_VERSION,
) -> SemanticEvidenceSnapshot:
    return SemanticEvidenceSnapshot(
        assessment_id=query_id * 10,
        query_source_article_id=query_id,
        query_document_id=query_id * 100,
        status=status,
        embedding_version=embedding_version,
        semantic_version=semantic_version,
        candidates=candidates,
        evaluation_count=1,
    )


def fixture_deterministic_evidence() -> tuple[DeterministicEvidence, ...]:
    classifications = {
        (100, 201): AssessmentType.EXACT_DUPLICATE,
        (100, 202): AssessmentType.MATERIAL_UPDATE,
        (100, 203): AssessmentType.RELATED_EVENT,
        (100, 204): AssessmentType.BROAD_TOPIC_OVERLAP,
        (100, 205): AssessmentType.BROAD_TOPIC_OVERLAP,
        (101, 206): AssessmentType.SAME_EVENT_DUPLICATE,
        (102, 208): AssessmentType.EXACT_DUPLICATE,
        (103, 209): AssessmentType.SAME_EVENT_DUPLICATE,
        (104, 210): AssessmentType.BROAD_TOPIC_OVERLAP,
    }
    return tuple(
        deterministic(query_id, candidate_id, classification)
        for (query_id, candidate_id), classification in classifications.items()
    )


def fixture_semantic_evidence() -> tuple[SemanticEvidenceSnapshot, ...]:
    return (
        semantic_snapshot(
            100,
            SemanticAssessmentStatus.RETRIEVED,
            (
                semantic_candidate(201, 1, 0.02),
                semantic_candidate(202, 2, 0.03),
                semantic_candidate(203, 3, 0.18),
                semantic_candidate(204, 4, 0.35),
                semantic_candidate(205, 5, 0.65),
            ),
        ),
        semantic_snapshot(
            101,
            SemanticAssessmentStatus.RETRIEVED,
            (
                semantic_candidate(206, 1, 0.08),
                semantic_candidate(207, 2, 0.30),
            ),
        ),
        semantic_snapshot(102, SemanticAssessmentStatus.NOT_READY),
        semantic_snapshot(103, SemanticAssessmentStatus.NO_CANDIDATES),
    )


def build_report(
    labels: ReviewedLabelSet | None = None,
    deterministic_evidence=None,
    semantic_evidence=None,
    *,
    selection: CalibrationSelection = SELECTION,
    cutoffs=(0.05, 0.1),
):
    labels = labels or load_reviewed_labels(FIXTURE_PATH)
    deterministic_evidence = (
        fixture_deterministic_evidence()
        if deterministic_evidence is None
        else deterministic_evidence
    )
    semantic_evidence = (
        fixture_semantic_evidence()
        if semantic_evidence is None
        else semantic_evidence
    )
    return calibrate(
        labels,
        InMemoryDeterministicEvidenceReader(deterministic_evidence),
        InMemorySemanticEvidenceReader(semantic_evidence),
        selection=selection,
        research_cutoffs=cutoffs,
    )


def report_row(report, query_id: int, candidate_id: int):
    return next(
        row
        for row in report.rows
        if row.query_source_article_id == query_id
        and row.candidate_source_article_id == candidate_id
    )


def retrieval_metrics(report, definition: RelevanceDefinition):
    return next(
        item
        for item in report.retrieval_metrics
        if item.relevance_definition is definition
    )


class ReviewedLabelTests(unittest.TestCase):
    def test_json_fixture_validates_all_relationship_labels(self):
        labels = load_reviewed_labels(FIXTURE_PATH)
        self.assertEqual(labels.schema_version, LABEL_SCHEMA_VERSION)
        self.assertEqual(
            {item.relationship_label for item in labels.relationships},
            set(RelationshipLabel),
        )

    def test_duplicate_label_pair_is_rejected(self):
        label = ReviewedLabel(1, 2, RelationshipLabel.EXACT_DUPLICATE)
        with self.assertRaisesRegex(ValueError, "pairs must be unique"):
            ReviewedLabelSet(LABEL_SCHEMA_VERSION, (label, label))

    def test_unsupported_label_is_rejected(self):
        payload = {
            "schema_version": LABEL_SCHEMA_VERSION,
            "relationships": [
                {
                    "query_source_article_id": 1,
                    "candidate_source_article_id": 2,
                    "relationship_label": "probably_same",
                }
            ],
        }
        with patch.object(Path, "read_text", return_value=json.dumps(payload)):
            with self.assertRaisesRegex(ValueError, "label is invalid"):
                load_reviewed_labels(Path("labels.json"))

    def test_jsonl_and_csv_formats_preserve_schema_version(self):
        row = {
            "schema_version": LABEL_SCHEMA_VERSION,
            "query_source_article_id": 1,
            "candidate_source_article_id": 2,
            "relationship_label": "related_event",
        }
        with patch.object(Path, "read_text", return_value=json.dumps(row) + "\n"):
            self.assertIsNone(
                load_reviewed_labels(Path("labels.jsonl")).relationships[0].reviewer_note
            )
        csv_text = (
            "schema_version,query_source_article_id,"
            "candidate_source_article_id,relationship_label\n"
            f"{LABEL_SCHEMA_VERSION},1,2,related_event\n"
        )
        with patch.object(Path, "open", mock_open(read_data=csv_text)):
            self.assertEqual(
                load_reviewed_labels(Path("labels.csv")).relationships[0].relationship_label,
                RelationshipLabel.RELATED_EVENT,
            )


class CalibrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = build_report()

    def test_calibration_joins_deterministic_and_semantic_evidence(self):
        row = report_row(self.report, 100, 201)
        self.assertEqual(row.human_label, RelationshipLabel.EXACT_DUPLICATE)
        self.assertEqual(row.deterministic_classification, AssessmentType.EXACT_DUPLICATE)
        self.assertTrue(row.semantic_available)
        self.assertEqual((row.semantic_rank, row.native_distance), (1, 0.02))
        self.assertEqual(row.matched_query_chunk_count, 2)

    def test_missing_deterministic_evidence_remains_explicit(self):
        row = report_row(self.report, 101, 207)
        self.assertFalse(row.deterministic_available)
        self.assertIsNone(row.deterministic_classification)
        self.assertTrue(row.semantic_available)

    def test_missing_semantic_assessment_remains_explicit(self):
        row = report_row(self.report, 104, 210)
        self.assertIsNone(row.semantic_status)
        self.assertFalse(row.semantic_available)
        self.assertEqual(row.semantic_missing_reason, "assessment_missing")

    def test_not_ready_is_not_converted_to_a_distance(self):
        row = report_row(self.report, 102, 208)
        self.assertEqual(row.semantic_status, SemanticAssessmentStatus.NOT_READY)
        self.assertIsNone(row.native_distance)
        self.assertEqual(row.semantic_missing_reason, "status_not_ready")

    def test_no_candidates_is_a_distinct_valid_miss(self):
        row = report_row(self.report, 103, 209)
        self.assertEqual(row.semantic_status, SemanticAssessmentStatus.NO_CANDIDATES)
        self.assertEqual(row.semantic_missing_reason, "no_candidates")
        strict = retrieval_metrics(self.report, RelevanceDefinition.STRICT_DUPLICATE)
        self.assertNotIn(103, strict.unavailable_queries)

    def test_distance_statistics_are_reported_by_label(self):
        exact = next(
            item
            for item in self.report.distance_distributions
            if item.label is RelationshipLabel.EXACT_DUPLICATE
        )
        self.assertEqual((exact.labeled_count, exact.retrieved_count), (2, 1))
        self.assertEqual(exact.missing_semantic_count, 1)
        self.assertEqual((exact.minimum_distance, exact.mean_distance), (0.02, 0.02))

    def test_median_and_percentiles_require_documented_sample_size(self):
        relationships = tuple(
            ReviewedLabel(1, candidate_id, RelationshipLabel.EXACT_DUPLICATE)
            for candidate_id in range(11, 16)
        )
        labels = ReviewedLabelSet(LABEL_SCHEMA_VERSION, relationships)
        candidates = tuple(
            semantic_candidate(candidate_id, rank, distance)
            for rank, (candidate_id, distance) in enumerate(
                zip(range(11, 16), (0.01, 0.02, 0.03, 0.04, 0.05)),
                start=1,
            )
        )
        report = build_report(
            labels,
            deterministic_evidence=(),
            semantic_evidence=(
                semantic_snapshot(1, SemanticAssessmentStatus.RETRIEVED, candidates),
            ),
        )
        exact = next(
            item
            for item in report.distance_distributions
            if item.label is RelationshipLabel.EXACT_DUPLICATE
        )
        self.assertTrue(exact.sufficient_sample)
        self.assertAlmostEqual(exact.median_distance, 0.03)
        self.assertAlmostEqual(exact.percentile_10, 0.014)
        self.assertAlmostEqual(exact.percentile_25, 0.02)
        self.assertAlmostEqual(exact.percentile_75, 0.04)
        self.assertAlmostEqual(exact.percentile_90, 0.046)

    def test_rank_distributions_are_explicit(self):
        material = next(
            item
            for item in self.report.rank_distributions
            if item.label is RelationshipLabel.MATERIAL_UPDATE
        )
        self.assertEqual((material.minimum_rank, material.maximum_rank), (2, 2))
        self.assertEqual(material.rank_counts[0].rank, 2)
        self.assertEqual(material.rank_counts[0].count, 1)

    def test_strict_recall_at_1_3_5_10(self):
        strict = retrieval_metrics(self.report, RelevanceDefinition.STRICT_DUPLICATE)
        self.assertEqual(strict.evaluable_query_count, 3)
        for metric in strict.recall_at_k:
            self.assertAlmostEqual(metric.recall, 2 / 3)

    def test_broader_recall_at_1_3_5_10(self):
        broader = retrieval_metrics(self.report, RelevanceDefinition.BROADER_SAME_EVENT)
        recalls = {item.k: item.recall for item in broader.recall_at_k}
        self.assertAlmostEqual(recalls[1], 0.5)
        for k in (3, 5, 10):
            self.assertAlmostEqual(recalls[k], 2 / 3)

    def test_strict_mrr(self):
        strict = retrieval_metrics(self.report, RelevanceDefinition.STRICT_DUPLICATE)
        self.assertAlmostEqual(strict.mean_reciprocal_rank, 2 / 3)

    def test_broader_mrr(self):
        broader = retrieval_metrics(self.report, RelevanceDefinition.BROADER_SAME_EVENT)
        self.assertAlmostEqual(broader.mean_reciprocal_rank, 2 / 3)

    def test_deterministic_confusion_matrix_preserves_multiclass_labels(self):
        comparison = self.report.deterministic_comparison
        self.assertEqual(comparison.available_count, 9)
        self.assertEqual(comparison.missing_count, 1)
        self.assertEqual(comparison.exact_agreement_count, 7)
        cell = next(
            item
            for item in comparison.cells
            if item.human_label is RelationshipLabel.UNRELATED
            and item.deterministic_classification == "broad_topic_overlap"
        )
        self.assertEqual(cell.count, 2)

    def test_distance_cutoff_metrics_are_research_only(self):
        metric = next(
            item
            for item in self.report.cutoff_research
            if item.relevance_definition is RelevanceDefinition.STRICT_DUPLICATE
            and item.cutoff == 0.05
        )
        self.assertEqual(
            (
                metric.true_positive_count,
                metric.false_positive_count,
                metric.false_negative_count,
            ),
            (1, 1, 2),
        )
        self.assertAlmostEqual(metric.precision, 0.5)
        self.assertAlmostEqual(metric.recall, 1 / 3)
        self.assertAlmostEqual(metric.f1, 0.4)
        self.assertEqual(metric.designation, "research_candidate_only")

    def test_material_update_overlap_is_flagged_without_suppression_advice(self):
        overlap = self.report.material_update_overlap
        self.assertEqual(overlap.overlapping_material_update_count, 1)
        self.assertEqual(overlap.at_or_below_strict_median_count, 1)
        self.assertEqual(overlap.overlapping_pairs, ((100, 202),))
        self.assertIn("distance_alone_is_insufficient", overlap.observation)

    def test_selected_versions_are_not_mixed(self):
        other_version = "openai:text-embedding-3-small:1536:chunk-v2"
        evidence = (
            *fixture_semantic_evidence(),
            semantic_snapshot(
                100,
                SemanticAssessmentStatus.RETRIEVED,
                (semantic_candidate(201, 1, 0.99),),
                embedding_version=other_version,
            ),
        )
        report = build_report(semantic_evidence=evidence)
        self.assertEqual(report_row(report, 100, 201).native_distance, 0.02)
        self.assertEqual(report.embedding_version, EMBEDDING_VERSION)

    def test_small_samples_emit_warnings_without_percentiles(self):
        exact = next(
            item
            for item in self.report.distance_distributions
            if item.label is RelationshipLabel.EXACT_DUPLICATE
        )
        self.assertFalse(exact.sufficient_sample)
        self.assertIsNone(exact.percentile_90)
        self.assertTrue(self.report.sample_warnings)
        self.assertIn("sample_size_too_small_for_policy", self.report.observations)

    def test_report_json_is_deterministic_and_contains_no_bodies_or_vectors(self):
        first = render_report_json(self.report)
        second = render_report_json(self.report)
        self.assertEqual(first, second)
        self.assertIn('"report_schema_version": "semantic-calibration-report-v1"', first)
        self.assertNotIn('"full_text"', first)
        self.assertNotIn('"chunk_text"', first)
        self.assertNotIn('"vector"', first)

    def test_calibration_makes_zero_provider_calls(self):
        with patch("embeddings.provider.OpenAIEmbeddingProvider") as provider:
            build_report()
        provider.assert_not_called()


class ReadOnlyEvidenceReaderTests(unittest.TestCase):
    def test_database_readers_execute_selects_without_commits(self):
        deterministic_cursor = Mock()
        deterministic_cursor.fetchall.return_value = [
            {
                "article_id": 100,
                "candidate_article_id": 201,
                "assessment_type": "exact_duplicate",
                "same_provider_article_id": 1,
                "same_event_id": 0,
                "same_canonical_url": 0,
                "same_content_hash": 1,
                "title_token_jaccard": 1,
                "publication_distance_hours": 0,
                "shared_entities_json": "[]",
                "shared_dates_json": "[]",
                "shared_numbers_json": "[]",
                "reason_json": '{"codes":["same_content_fingerprint"]}',
                "policy_version": POLICY_VERSION,
            }
        ]
        deterministic_connection = Mock()
        deterministic_connection.cursor.return_value = deterministic_cursor
        deterministic_reader = MariaDBDeterministicEvidenceReader(
            connect=lambda: deterministic_connection
        )
        loaded = deterministic_reader.load_evidence(
            ((100, 201),),
            policy_version=POLICY_VERSION,
        )
        self.assertIn((100, 201), loaded)
        self.assertTrue(deterministic_cursor.execute.call_args.args[0].lstrip().startswith("SELECT"))
        deterministic_connection.commit.assert_not_called()
        deterministic_connection.rollback.assert_not_called()

        semantic_cursor = Mock()
        semantic_cursor.fetchall.return_value = [
            {
                "id": 1,
                "query_source_article_id": 100,
                "query_document_id": 1000,
                "status": "retrieved",
                "embedding_version": EMBEDDING_VERSION,
                "semantic_version": SEMANTIC_VERSION,
                "evidence_json": json.dumps(
                    [
                        {
                            "candidate_source_article_id": 201,
                            "candidate_document_id": 2010,
                            "native_distance": 0.02,
                            "best_query_chunk_index": 0,
                            "best_candidate_chunk_index": 0,
                            "matched_query_chunk_count": 1,
                            "publication_delta_hours": 1.0,
                        }
                    ]
                ),
                "evaluation_count": 1,
                "updated_at": "2026-09-01 12:00:00",
            }
        ]
        semantic_connection = Mock()
        semantic_connection.cursor.return_value = semantic_cursor
        semantic_reader = MariaDBSemanticEvidenceReader(
            connect=lambda: semantic_connection
        )
        loaded_semantic = semantic_reader.load_evidence(
            (100,),
            embedding_version=EMBEDDING_VERSION,
            semantic_version=SEMANTIC_VERSION,
        )
        self.assertEqual(loaded_semantic[100].candidates[0].rank, 1)
        self.assertTrue(semantic_cursor.execute.call_args.args[0].lstrip().startswith("SELECT"))
        semantic_connection.commit.assert_not_called()
        semantic_connection.rollback.assert_not_called()

    def test_no_runtime_threshold_or_pipeline_import_is_added(self):
        config_source = (PROJECT_DIR / "config.py").read_text(encoding="utf-8")
        env_example = (REPOSITORY_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("SEMANTIC_THRESHOLD", config_source + env_example)
        for relative_path in (
            "GetNewsAPI/gpt_processor.py",
            "GetNewsAPI/tasks.py",
            "GetNewsAPI/duplicate_detection/policy.py",
            "GetNewsAPI/duplicate_detection/shadow.py",
            "GetNewsAPI/semantic_retrieval/shadow.py",
        ):
            source = (REPOSITORY_DIR / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("semantic_calibration", source, relative_path)


if __name__ == "__main__":
    unittest.main()
