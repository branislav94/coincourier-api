"""Offline comparison of reviewed labels with existing evidence snapshots."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from statistics import mean, median

from semantic_retrieval.evaluation import (
    RELEVANCE_LABELS,
    RelationshipLabel,
    RelevanceDefinition,
)
from vector_store.semantic_assessments import SemanticAssessmentStatus

from .models import (
    DEFAULT_RESEARCH_CUTOFFS,
    MIN_RELIABLE_SAMPLE_SIZE,
    REPORT_SCHEMA_VERSION,
    AvailabilityCounts,
    CalibrationReport,
    CalibrationRow,
    CalibrationSelection,
    ConfusionCell,
    CutoffResearchMetric,
    DeterministicComparison,
    DistanceDistribution,
    LabelCount,
    MaterialUpdateOverlap,
    RankCount,
    RankDistribution,
    RecallAtK,
    RetrievalMetrics,
    ReviewedLabelSet,
    SemanticEvidenceSnapshot,
    SignalComparisonCount,
)
from .readers import DeterministicEvidenceReader, SemanticEvidenceReader


RECALL_LEVELS = (1, 3, 5, 10)
EVALUABLE_SEMANTIC_STATUSES = {
    SemanticAssessmentStatus.RETRIEVED,
    SemanticAssessmentStatus.NO_CANDIDATES,
}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _semantic_candidate(
    snapshot: SemanticEvidenceSnapshot | None,
    candidate_source_article_id: int,
):
    if snapshot is None or snapshot.status is not SemanticAssessmentStatus.RETRIEVED:
        return None
    return next(
        (
            candidate
            for candidate in snapshot.candidates
            if candidate.candidate_source_article_id == candidate_source_article_id
        ),
        None,
    )


def _semantic_missing_reason(
    snapshot: SemanticEvidenceSnapshot | None,
    candidate_found: bool,
) -> str | None:
    if candidate_found:
        return None
    if snapshot is None:
        return "assessment_missing"
    if snapshot.status is SemanticAssessmentStatus.NO_CANDIDATES:
        return "no_candidates"
    if snapshot.status is SemanticAssessmentStatus.RETRIEVED:
        return "candidate_not_retrieved"
    return f"status_{snapshot.status.value}"


def _signal_comparison(
    deterministic_classification,
    semantic_available: bool,
    semantic_missing_reason: str | None,
) -> str:
    if deterministic_classification is None:
        deterministic_state = "deterministic_missing"
    else:
        deterministic_state = f"deterministic_{deterministic_classification.value}"
    semantic_state = (
        "semantic_candidate_retrieved"
        if semantic_available
        else f"semantic_{semantic_missing_reason}"
    )
    return f"{deterministic_state}__{semantic_state}"


def _rows(
    labels: ReviewedLabelSet,
    deterministic_evidence,
    semantic_evidence,
    selection: CalibrationSelection,
) -> tuple[CalibrationRow, ...]:
    rows = []
    for label in labels.relationships:
        pair = (label.query_source_article_id, label.candidate_source_article_id)
        deterministic = deterministic_evidence.get(pair)
        if deterministic and deterministic.policy_version != selection.deterministic_policy_version:
            raise ValueError("deterministic evidence version isolation failed")
        semantic = semantic_evidence.get(label.query_source_article_id)
        if semantic and (
            semantic.embedding_version != selection.embedding_version
            or semantic.semantic_version != selection.semantic_version
        ):
            raise ValueError("semantic evidence version isolation failed")
        candidate = _semantic_candidate(semantic, label.candidate_source_article_id)
        missing_reason = _semantic_missing_reason(semantic, candidate is not None)
        rows.append(
            CalibrationRow(
                query_source_article_id=label.query_source_article_id,
                candidate_source_article_id=label.candidate_source_article_id,
                human_label=label.relationship_label,
                deterministic_available=deterministic is not None,
                deterministic_classification=(
                    deterministic.classification if deterministic else None
                ),
                deterministic_policy_version=selection.deterministic_policy_version,
                same_provider_article_id=(
                    deterministic.same_provider_article_id if deterministic else None
                ),
                same_event_id=deterministic.same_event_id if deterministic else None,
                same_canonical_url=(
                    deterministic.same_canonical_url if deterministic else None
                ),
                same_content_hash=(
                    deterministic.same_content_hash if deterministic else None
                ),
                title_token_jaccard=(
                    deterministic.title_token_jaccard if deterministic else None
                ),
                deterministic_publication_distance_hours=(
                    deterministic.publication_distance_hours if deterministic else None
                ),
                deterministic_reason_codes=(
                    deterministic.reason_codes if deterministic else ()
                ),
                semantic_status=semantic.status if semantic else None,
                semantic_available=candidate is not None,
                semantic_missing_reason=missing_reason,
                semantic_rank=candidate.rank if candidate else None,
                native_distance=candidate.native_distance if candidate else None,
                best_query_chunk_index=(
                    candidate.best_query_chunk_index if candidate else None
                ),
                best_candidate_chunk_index=(
                    candidate.best_candidate_chunk_index if candidate else None
                ),
                matched_query_chunk_count=(
                    candidate.matched_query_chunk_count if candidate else None
                ),
                publication_delta_hours=(
                    candidate.publication_delta_hours if candidate else None
                ),
                query_document_id=semantic.query_document_id if semantic else None,
                embedding_version=selection.embedding_version,
                semantic_version=selection.semantic_version,
                signal_comparison=_signal_comparison(
                    deterministic.classification if deterministic else None,
                    candidate is not None,
                    missing_reason,
                ),
            )
        )
    return tuple(rows)


def _availability(rows: tuple[CalibrationRow, ...]) -> AvailabilityCounts:
    return AvailabilityCounts(
        labeled_pair_count=len(rows),
        deterministic_available_count=sum(row.deterministic_available for row in rows),
        deterministic_missing_count=sum(not row.deterministic_available for row in rows),
        semantic_candidate_available_count=sum(row.semantic_available for row in rows),
        semantic_candidate_missing_count=sum(not row.semantic_available for row in rows),
        semantic_assessment_missing_count=sum(
            row.semantic_missing_reason == "assessment_missing" for row in rows
        ),
        semantic_unavailable_status_count=sum(
            bool(row.semantic_missing_reason)
            and row.semantic_missing_reason.startswith("status_")
            for row in rows
        ),
        semantic_no_candidates_count=sum(
            row.semantic_missing_reason == "no_candidates" for row in rows
        ),
        semantic_candidate_not_retrieved_count=sum(
            row.semantic_missing_reason == "candidate_not_retrieved" for row in rows
        ),
    )


def _deterministic_comparison(
    rows: tuple[CalibrationRow, ...],
) -> DeterministicComparison:
    cells = Counter(
        (
            row.human_label,
            (
                row.deterministic_classification.value
                if row.deterministic_classification
                else "missing"
            ),
        )
        for row in rows
    )
    comparisons = Counter(row.signal_comparison for row in rows)
    return DeterministicComparison(
        available_count=sum(row.deterministic_available for row in rows),
        missing_count=sum(not row.deterministic_available for row in rows),
        exact_agreement_count=sum(
            row.deterministic_classification is not None
            and row.deterministic_classification.value == row.human_label.value
            for row in rows
        ),
        cells=tuple(
            ConfusionCell(label, classification, count)
            for (label, classification), count in sorted(
                cells.items(),
                key=lambda item: (item[0][0].value, item[0][1]),
            )
        ),
        signal_comparisons=tuple(
            SignalComparisonCount(category, count)
            for category, count in sorted(comparisons.items())
        ),
    )


def _distance_distributions(
    rows: tuple[CalibrationRow, ...],
) -> tuple[DistanceDistribution, ...]:
    distributions = []
    for label in sorted(RelationshipLabel, key=lambda item: item.value):
        labeled = [row for row in rows if row.human_label is label]
        distances = [
            row.native_distance
            for row in labeled
            if row.native_distance is not None
        ]
        sufficient = len(distances) >= MIN_RELIABLE_SAMPLE_SIZE
        distributions.append(
            DistanceDistribution(
                label=label,
                labeled_count=len(labeled),
                retrieved_count=len(distances),
                missing_semantic_count=len(labeled) - len(distances),
                minimum_distance=min(distances) if distances else None,
                maximum_distance=max(distances) if distances else None,
                mean_distance=mean(distances) if distances else None,
                median_distance=median(distances) if distances else None,
                percentile_10=_percentile(distances, 0.10) if sufficient else None,
                percentile_25=_percentile(distances, 0.25) if sufficient else None,
                percentile_75=_percentile(distances, 0.75) if sufficient else None,
                percentile_90=_percentile(distances, 0.90) if sufficient else None,
                sufficient_sample=sufficient,
            )
        )
    return tuple(distributions)


def _rank_distributions(
    rows: tuple[CalibrationRow, ...],
) -> tuple[RankDistribution, ...]:
    distributions = []
    for label in sorted(RelationshipLabel, key=lambda item: item.value):
        ranks = [row.semantic_rank for row in rows if row.human_label is label]
        present = [rank for rank in ranks if rank is not None]
        rank_counts = Counter(present)
        distributions.append(
            RankDistribution(
                label=label,
                retrieved_count=len(present),
                minimum_rank=min(present) if present else None,
                maximum_rank=max(present) if present else None,
                mean_rank=mean(present) if present else None,
                median_rank=median(present) if present else None,
                rank_counts=tuple(
                    RankCount(rank, count) for rank, count in sorted(rank_counts.items())
                ),
            )
        )
    return tuple(distributions)


def _retrieval_metrics(
    rows: tuple[CalibrationRow, ...],
    semantic_evidence,
    query_ids: tuple[int, ...],
    definition: RelevanceDefinition,
) -> RetrievalMetrics:
    relevant_labels = RELEVANCE_LABELS[definition]
    rows_by_query = defaultdict(list)
    for row in rows:
        rows_by_query[row.query_source_article_id].append(row)

    recall_values = {k: [] for k in RECALL_LEVELS}
    reciprocal_ranks = []
    unavailable_queries = []
    evaluable_queries = 0
    for query_id in query_ids:
        relevant = [
            row for row in rows_by_query[query_id] if row.human_label in relevant_labels
        ]
        if not relevant:
            continue
        snapshot = semantic_evidence.get(query_id)
        if snapshot is None or snapshot.status not in EVALUABLE_SEMANTIC_STATUSES:
            unavailable_queries.append(query_id)
            continue
        evaluable_queries += 1
        for k in RECALL_LEVELS:
            found = sum(
                row.semantic_rank is not None and row.semantic_rank <= k
                for row in relevant
            )
            recall_values[k].append(found / len(relevant))
        first_rank = min(
            (row.semantic_rank for row in relevant if row.semantic_rank is not None),
            default=None,
        )
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)

    return RetrievalMetrics(
        relevance_definition=definition,
        query_count=len(query_ids),
        evaluable_query_count=evaluable_queries,
        unavailable_queries=tuple(unavailable_queries),
        recall_at_k=tuple(
            RecallAtK(k, mean(recall_values[k]) if recall_values[k] else 0.0)
            for k in RECALL_LEVELS
        ),
        mean_reciprocal_rank=(mean(reciprocal_ranks) if reciprocal_ranks else 0.0),
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _cutoff_research(
    rows: tuple[CalibrationRow, ...],
    cutoffs: tuple[float, ...],
    definition: RelevanceDefinition,
) -> tuple[CutoffResearchMetric, ...]:
    relevant_labels = RELEVANCE_LABELS[definition]
    evaluable = [
        row
        for row in rows
        if row.semantic_status in EVALUABLE_SEMANTIC_STATUSES
    ]
    metrics = []
    for cutoff in cutoffs:
        true_positive = false_positive = false_negative = 0
        for row in evaluable:
            expected = row.human_label in relevant_labels
            predicted = bool(
                row.native_distance is not None and row.native_distance <= cutoff
            )
            true_positive += expected and predicted
            false_positive += not expected and predicted
            false_negative += expected and not predicted
        precision = _ratio(true_positive, true_positive + false_positive)
        recall = _ratio(true_positive, true_positive + false_negative)
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else (0.0 if precision == 0.0 and recall == 0.0 else None)
        )
        metrics.append(
            CutoffResearchMetric(
                relevance_definition=definition,
                cutoff=cutoff,
                evaluable_pair_count=len(evaluable),
                true_positive_count=true_positive,
                false_positive_count=false_positive,
                false_negative_count=false_negative,
                precision=precision,
                recall=recall,
                f1=f1,
            )
        )
    return tuple(metrics)


def _material_update_overlap(
    rows: tuple[CalibrationRow, ...],
) -> MaterialUpdateOverlap:
    strict_labels = RELEVANCE_LABELS[RelevanceDefinition.STRICT_DUPLICATE]
    strict_distances = [
        row.native_distance
        for row in rows
        if row.human_label in strict_labels and row.native_distance is not None
    ]
    material_rows = [
        row for row in rows if row.human_label is RelationshipLabel.MATERIAL_UPDATE
    ]
    material_with_distance = [
        row for row in material_rows if row.native_distance is not None
    ]
    strict_minimum = min(strict_distances) if strict_distances else None
    strict_maximum = max(strict_distances) if strict_distances else None
    strict_median = median(strict_distances) if strict_distances else None
    overlapping = [
        row
        for row in material_with_distance
        if strict_minimum is not None
        and strict_maximum is not None
        and strict_minimum <= row.native_distance <= strict_maximum
    ]
    at_or_below_median = sum(
        strict_median is not None and row.native_distance <= strict_median
        for row in material_with_distance
    )
    sufficient = (
        len(strict_distances) >= MIN_RELIABLE_SAMPLE_SIZE
        and len(material_with_distance) >= MIN_RELIABLE_SAMPLE_SIZE
    )
    if not material_with_distance:
        observation = "material_update_semantic_evidence_missing"
    elif not strict_distances:
        observation = "strict_duplicate_semantic_evidence_missing"
    elif overlapping:
        observation = "material_update_distance_overlap_observed_distance_alone_is_insufficient"
    else:
        observation = "no_material_update_overlap_observed_in_this_sample"
    return MaterialUpdateOverlap(
        labeled_count=len(material_rows),
        retrieved_count=len(material_with_distance),
        strict_retrieved_count=len(strict_distances),
        strict_minimum_distance=strict_minimum,
        strict_maximum_distance=strict_maximum,
        strict_median_distance=strict_median,
        overlapping_material_update_count=len(overlapping),
        at_or_below_strict_median_count=at_or_below_median,
        overlapping_pairs=tuple(
            (row.query_source_article_id, row.candidate_source_article_id)
            for row in overlapping
        ),
        sufficient_sample=sufficient,
        observation=observation,
    )


def calibrate(
    labels: ReviewedLabelSet,
    deterministic_reader: DeterministicEvidenceReader,
    semantic_reader: SemanticEvidenceReader,
    *,
    selection: CalibrationSelection,
    research_cutoffs=DEFAULT_RESEARCH_CUTOFFS,
) -> CalibrationReport:
    """Build an advisory-only report from existing evidence and reviewed labels."""
    cutoffs = tuple(sorted({float(value) for value in research_cutoffs}))
    if not cutoffs or len(cutoffs) > 20 or any(
        not math.isfinite(value) or value < 0 for value in cutoffs
    ):
        raise ValueError("research cutoffs must be 1-20 finite non-negative values")

    pairs = tuple(
        (item.query_source_article_id, item.candidate_source_article_id)
        for item in labels.relationships
    )
    deterministic_evidence = deterministic_reader.load_evidence(
        pairs,
        policy_version=selection.deterministic_policy_version,
    )
    semantic_evidence = semantic_reader.load_evidence(
        labels.query_source_article_ids,
        embedding_version=selection.embedding_version,
        semantic_version=selection.semantic_version,
    )
    rows = _rows(labels, deterministic_evidence, semantic_evidence, selection)
    distributions = _distance_distributions(rows)
    warnings = tuple(
        f"label={item.label.value} retrieved_count={item.retrieved_count} "
        f"is below confidence minimum={MIN_RELIABLE_SAMPLE_SIZE}"
        for item in distributions
        if item.labeled_count and not item.sufficient_sample
    )
    material_overlap = _material_update_overlap(rows)
    metrics = tuple(
        _retrieval_metrics(
            rows,
            semantic_evidence,
            labels.query_source_article_ids,
            definition,
        )
        for definition in RelevanceDefinition
    )
    cutoff_metrics = tuple(
        metric
        for definition in RelevanceDefinition
        for metric in _cutoff_research(rows, cutoffs, definition)
    )
    observations = [
        "research_only_no_runtime_policy",
        "selected_versions_are_analyzed_without_cross_version_averaging",
        material_overlap.observation,
    ]
    if warnings:
        observations.append("sample_size_too_small_for_policy")

    return CalibrationReport(
        report_schema_version=REPORT_SCHEMA_VERSION,
        label_schema_version=labels.schema_version,
        deterministic_policy_version=selection.deterministic_policy_version,
        embedding_version=selection.embedding_version,
        semantic_version=selection.semantic_version,
        query_count=len(labels.query_source_article_ids),
        labeled_pair_count=len(rows),
        label_counts=tuple(
            LabelCount(label, sum(row.human_label is label for row in rows))
            for label in sorted(RelationshipLabel, key=lambda item: item.value)
        ),
        availability=_availability(rows),
        rows=rows,
        deterministic_comparison=_deterministic_comparison(rows),
        distance_distributions=distributions,
        rank_distributions=_rank_distributions(rows),
        retrieval_metrics=metrics,
        cutoff_research=cutoff_metrics,
        material_update_overlap=material_overlap,
        sample_warnings=warnings,
        observations=tuple(observations),
    )
