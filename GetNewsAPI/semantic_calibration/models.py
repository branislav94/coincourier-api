"""Typed, read-only contracts for Phase 6C2B calibration analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from duplicate_detection.policy import AssessmentType
from semantic_retrieval.evaluation import RelationshipLabel, RelevanceDefinition
from vector_store.semantic_assessments import SemanticAssessmentStatus


LABEL_SCHEMA_VERSION = "semantic-calibration-labels-v1"
REPORT_SCHEMA_VERSION = "semantic-calibration-report-v1"
MIN_RELIABLE_SAMPLE_SIZE = 5
DEFAULT_RESEARCH_CUTOFFS = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5)


@dataclass(frozen=True)
class ReviewedLabel:
    query_source_article_id: int
    candidate_source_article_id: int
    relationship_label: RelationshipLabel
    reviewer_note: str | None = None
    reviewed_at: str | None = None

    def __post_init__(self) -> None:
        if (
            self.query_source_article_id <= 0
            or self.candidate_source_article_id <= 0
            or self.query_source_article_id == self.candidate_source_article_id
        ):
            raise ValueError("reviewed label IDs must be positive and distinct")
        if not isinstance(self.relationship_label, RelationshipLabel):
            raise ValueError("reviewed relationship label is invalid")
        if self.reviewer_note is not None and len(self.reviewer_note) > 500:
            raise ValueError("reviewer_note must be 500 characters or fewer")
        if self.reviewed_at is not None:
            try:
                datetime.fromisoformat(self.reviewed_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("reviewed_at must be an ISO-8601 timestamp") from exc


@dataclass(frozen=True)
class ReviewedLabelSet:
    schema_version: str
    relationships: tuple[ReviewedLabel, ...]

    def __post_init__(self) -> None:
        if self.schema_version != LABEL_SCHEMA_VERSION:
            raise ValueError("unsupported reviewed-label schema version")
        if not self.relationships:
            raise ValueError("reviewed-label set requires relationships")
        identities = [
            (item.query_source_article_id, item.candidate_source_article_id)
            for item in self.relationships
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("reviewed label pairs must be unique")
        object.__setattr__(
            self,
            "relationships",
            tuple(
                sorted(
                    self.relationships,
                    key=lambda item: (
                        item.query_source_article_id,
                        item.candidate_source_article_id,
                        item.relationship_label.value,
                    ),
                )
            ),
        )

    @property
    def query_source_article_ids(self) -> tuple[int, ...]:
        return tuple(
            sorted({item.query_source_article_id for item in self.relationships})
        )


@dataclass(frozen=True)
class CalibrationSelection:
    deterministic_policy_version: str
    embedding_version: str
    semantic_version: str

    def __post_init__(self) -> None:
        for name, value in (
            ("deterministic_policy_version", self.deterministic_policy_version),
            ("embedding_version", self.embedding_version),
            ("semantic_version", self.semantic_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")


@dataclass(frozen=True)
class DeterministicEvidence:
    query_source_article_id: int
    candidate_source_article_id: int
    classification: AssessmentType
    same_provider_article_id: bool
    same_event_id: bool
    same_canonical_url: bool
    same_content_hash: bool
    title_token_jaccard: float
    publication_distance_hours: float | None
    shared_entities: tuple[str, ...]
    shared_dates: tuple[str, ...]
    shared_numbers: tuple[str, ...]
    reason_codes: tuple[str, ...]
    policy_version: str

    def __post_init__(self) -> None:
        if (
            self.query_source_article_id <= 0
            or self.candidate_source_article_id <= 0
            or self.query_source_article_id == self.candidate_source_article_id
        ):
            raise ValueError("deterministic evidence IDs must be positive and distinct")
        if not isinstance(self.classification, AssessmentType):
            raise ValueError("deterministic classification is invalid")
        if not math.isfinite(self.title_token_jaccard) or not 0 <= self.title_token_jaccard <= 1:
            raise ValueError("title_token_jaccard must be between zero and one")
        if (
            self.publication_distance_hours is not None
            and (
                not math.isfinite(self.publication_distance_hours)
                or self.publication_distance_hours < 0
            )
        ):
            raise ValueError("publication distance must be finite and non-negative")
        if not self.policy_version.strip():
            raise ValueError("deterministic policy version is required")


@dataclass(frozen=True)
class SemanticCandidateEvidence:
    candidate_source_article_id: int
    candidate_document_id: int
    rank: int
    native_distance: float
    best_query_chunk_index: int
    best_candidate_chunk_index: int
    matched_query_chunk_count: int
    publication_delta_hours: float

    def __post_init__(self) -> None:
        if self.candidate_source_article_id <= 0 or self.candidate_document_id <= 0:
            raise ValueError("semantic candidate identities must be positive")
        if self.rank <= 0:
            raise ValueError("semantic candidate rank must be positive")
        if not math.isfinite(self.native_distance):
            raise ValueError("semantic candidate distance must be finite")
        if self.best_query_chunk_index < 0 or self.best_candidate_chunk_index < 0:
            raise ValueError("semantic candidate chunk indexes cannot be negative")
        if self.matched_query_chunk_count <= 0:
            raise ValueError("matched query chunk count must be positive")
        if not math.isfinite(self.publication_delta_hours):
            raise ValueError("publication delta must be finite")


@dataclass(frozen=True)
class SemanticEvidenceSnapshot:
    assessment_id: int
    query_source_article_id: int
    query_document_id: int | None
    status: SemanticAssessmentStatus
    embedding_version: str
    semantic_version: str
    candidates: tuple[SemanticCandidateEvidence, ...]
    evaluation_count: int

    def __post_init__(self) -> None:
        if self.assessment_id <= 0 or self.query_source_article_id <= 0:
            raise ValueError("semantic assessment identities must be positive")
        if self.query_document_id is not None and self.query_document_id <= 0:
            raise ValueError("semantic query document identity must be positive")
        if not self.embedding_version.strip() or not self.semantic_version.strip():
            raise ValueError("semantic evidence versions are required")
        if self.status is SemanticAssessmentStatus.RETRIEVED and not self.candidates:
            raise ValueError("retrieved semantic evidence requires candidates")
        if self.status is not SemanticAssessmentStatus.RETRIEVED and self.candidates:
            raise ValueError("unavailable semantic evidence cannot contain candidates")
        candidate_ids = [item.candidate_source_article_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("semantic candidate source identities must be unique")


@dataclass(frozen=True)
class CalibrationRow:
    query_source_article_id: int
    candidate_source_article_id: int
    human_label: RelationshipLabel
    deterministic_available: bool
    deterministic_classification: AssessmentType | None
    deterministic_policy_version: str
    same_provider_article_id: bool | None
    same_event_id: bool | None
    same_canonical_url: bool | None
    same_content_hash: bool | None
    title_token_jaccard: float | None
    deterministic_publication_distance_hours: float | None
    deterministic_reason_codes: tuple[str, ...]
    semantic_status: SemanticAssessmentStatus | None
    semantic_available: bool
    semantic_missing_reason: str | None
    semantic_rank: int | None
    native_distance: float | None
    best_query_chunk_index: int | None
    best_candidate_chunk_index: int | None
    matched_query_chunk_count: int | None
    publication_delta_hours: float | None
    query_document_id: int | None
    embedding_version: str
    semantic_version: str
    signal_comparison: str


@dataclass(frozen=True)
class LabelCount:
    label: RelationshipLabel
    count: int


@dataclass(frozen=True)
class AvailabilityCounts:
    labeled_pair_count: int
    deterministic_available_count: int
    deterministic_missing_count: int
    semantic_candidate_available_count: int
    semantic_candidate_missing_count: int
    semantic_assessment_missing_count: int
    semantic_unavailable_status_count: int
    semantic_no_candidates_count: int
    semantic_candidate_not_retrieved_count: int


@dataclass(frozen=True)
class ConfusionCell:
    human_label: RelationshipLabel
    deterministic_classification: str
    count: int


@dataclass(frozen=True)
class SignalComparisonCount:
    category: str
    count: int


@dataclass(frozen=True)
class DeterministicComparison:
    available_count: int
    missing_count: int
    exact_agreement_count: int
    cells: tuple[ConfusionCell, ...]
    signal_comparisons: tuple[SignalComparisonCount, ...]


@dataclass(frozen=True)
class DistanceDistribution:
    label: RelationshipLabel
    labeled_count: int
    retrieved_count: int
    missing_semantic_count: int
    minimum_distance: float | None
    maximum_distance: float | None
    mean_distance: float | None
    median_distance: float | None
    percentile_10: float | None
    percentile_25: float | None
    percentile_75: float | None
    percentile_90: float | None
    sufficient_sample: bool


@dataclass(frozen=True)
class RankCount:
    rank: int
    count: int


@dataclass(frozen=True)
class RankDistribution:
    label: RelationshipLabel
    retrieved_count: int
    minimum_rank: int | None
    maximum_rank: int | None
    mean_rank: float | None
    median_rank: float | None
    rank_counts: tuple[RankCount, ...]


@dataclass(frozen=True)
class RecallAtK:
    k: int
    recall: float


@dataclass(frozen=True)
class RetrievalMetrics:
    relevance_definition: RelevanceDefinition
    query_count: int
    evaluable_query_count: int
    unavailable_queries: tuple[int, ...]
    recall_at_k: tuple[RecallAtK, ...]
    mean_reciprocal_rank: float


@dataclass(frozen=True)
class CutoffResearchMetric:
    relevance_definition: RelevanceDefinition
    cutoff: float
    evaluable_pair_count: int
    true_positive_count: int
    false_positive_count: int
    false_negative_count: int
    precision: float | None
    recall: float | None
    f1: float | None
    designation: str = "research_candidate_only"


@dataclass(frozen=True)
class MaterialUpdateOverlap:
    labeled_count: int
    retrieved_count: int
    strict_retrieved_count: int
    strict_minimum_distance: float | None
    strict_maximum_distance: float | None
    strict_median_distance: float | None
    overlapping_material_update_count: int
    at_or_below_strict_median_count: int
    overlapping_pairs: tuple[tuple[int, int], ...]
    sufficient_sample: bool
    observation: str


@dataclass(frozen=True)
class CalibrationReport:
    report_schema_version: str
    label_schema_version: str
    deterministic_policy_version: str
    embedding_version: str
    semantic_version: str
    query_count: int
    labeled_pair_count: int
    label_counts: tuple[LabelCount, ...]
    availability: AvailabilityCounts
    rows: tuple[CalibrationRow, ...]
    deterministic_comparison: DeterministicComparison
    distance_distributions: tuple[DistanceDistribution, ...]
    rank_distributions: tuple[RankDistribution, ...]
    retrieval_metrics: tuple[RetrievalMetrics, ...]
    cutoff_research: tuple[CutoffResearchMetric, ...]
    material_update_overlap: MaterialUpdateOverlap
    sample_warnings: tuple[str, ...]
    observations: tuple[str, ...]
