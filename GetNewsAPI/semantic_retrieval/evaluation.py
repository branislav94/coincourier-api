"""Deterministic labeled evaluation for semantic retrieval results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .models import (
    MAX_SEMANTIC_TOP_K,
    SemanticRetrievalResult,
    SemanticRetrievalStatus,
)


EVALUATION_SCHEMA_VERSION = "semantic-eval-v1"


class RelationshipLabel(str, Enum):
    EXACT_DUPLICATE = "exact_duplicate"
    SAME_EVENT_DUPLICATE = "same_event_duplicate"
    MATERIAL_UPDATE = "material_update"
    RELATED_EVENT = "related_event"
    BROAD_TOPIC_OVERLAP = "broad_topic_overlap"
    UNRELATED = "unrelated"


class RelevanceDefinition(str, Enum):
    STRICT_DUPLICATE = "strict_duplicate"
    BROADER_SAME_EVENT = "broader_same_event"


RELEVANCE_LABELS = {
    RelevanceDefinition.STRICT_DUPLICATE: frozenset(
        {
            RelationshipLabel.EXACT_DUPLICATE,
            RelationshipLabel.SAME_EVENT_DUPLICATE,
        }
    ),
    RelevanceDefinition.BROADER_SAME_EVENT: frozenset(
        {
            RelationshipLabel.EXACT_DUPLICATE,
            RelationshipLabel.SAME_EVENT_DUPLICATE,
            RelationshipLabel.MATERIAL_UPDATE,
        }
    ),
}


@dataclass(frozen=True)
class LabeledRelationship:
    query_source_article_id: int
    candidate_source_article_id: int
    label: RelationshipLabel


@dataclass(frozen=True)
class EvaluationFixture:
    schema_version: str
    relationships: tuple[LabeledRelationship, ...]

    @property
    def query_source_article_ids(self) -> tuple[int, ...]:
        return tuple(
            sorted({item.query_source_article_id for item in self.relationships})
        )


@dataclass(frozen=True)
class RelationshipEvaluation:
    query_source_article_id: int
    candidate_source_article_id: int
    label: RelationshipLabel
    candidate_rank: int | None
    native_distance: float | None


@dataclass(frozen=True)
class LabelDistanceDistribution:
    label: RelationshipLabel
    labeled_count: int
    retrieved_count: int
    minimum_distance: float | None
    maximum_distance: float | None
    mean_distance: float | None


@dataclass(frozen=True)
class SemanticEvaluationMetrics:
    schema_version: str
    relevance_definition: RelevanceDefinition
    relevant_labels: tuple[RelationshipLabel, ...]
    top_k: int
    query_count: int
    evaluable_query_count: int
    evaluable_labeled_pair_count: int
    recall_at_k: float
    mean_reciprocal_rank: float
    top_k_labeled_coverage: float
    relationships: tuple[RelationshipEvaluation, ...]
    label_distributions: tuple[LabelDistanceDistribution, ...]
    missing_labeled_pairs: tuple[RelationshipEvaluation, ...]
    unavailable_queries: tuple[int, ...]


class SemanticRetriever(Protocol):
    def retrieve_source_neighbors(
        self,
        source_article_id: int,
        *,
        top_k: int | None = None,
    ) -> SemanticRetrievalResult:
        ...


def load_evaluation_fixture(path: str | Path) -> EvaluationFixture:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("semantic evaluation fixture must be a JSON object")
    schema_version = str(payload.get("schema_version") or "")
    if schema_version != EVALUATION_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported semantic evaluation schema: {schema_version or 'missing'}"
        )
    raw_relationships = payload.get("relationships")
    if not isinstance(raw_relationships, list) or not raw_relationships:
        raise ValueError("semantic evaluation fixture requires relationships")

    relationships: list[LabeledRelationship] = []
    identities: set[tuple[int, int]] = set()
    for raw in raw_relationships:
        if not isinstance(raw, dict):
            raise ValueError("semantic relationship entries must be JSON objects")
        query_id = int(raw.get("query_source_article_id", 0))
        candidate_id = int(raw.get("candidate_source_article_id", 0))
        if query_id <= 0 or candidate_id <= 0 or query_id == candidate_id:
            raise ValueError("semantic relationship IDs must be positive and distinct")
        identity = (query_id, candidate_id)
        if identity in identities:
            raise ValueError("semantic evaluation relationship pairs must be unique")
        identities.add(identity)
        try:
            label = RelationshipLabel(str(raw.get("label") or ""))
        except ValueError as exc:
            raise ValueError("semantic evaluation relationship label is invalid") from exc
        relationships.append(
            LabeledRelationship(
                query_source_article_id=query_id,
                candidate_source_article_id=candidate_id,
                label=label,
            )
        )

    return EvaluationFixture(
        schema_version=schema_version,
        relationships=tuple(
            sorted(
                relationships,
                key=lambda item: (
                    item.query_source_article_id,
                    item.candidate_source_article_id,
                    item.label.value,
                ),
            )
        ),
    )


def evaluate_retrieval(
    fixture: EvaluationFixture,
    retriever: SemanticRetriever,
    *,
    top_k: int,
    relevance_definition: RelevanceDefinition,
) -> SemanticEvaluationMetrics:
    if fixture.schema_version != EVALUATION_SCHEMA_VERSION:
        raise ValueError("unsupported semantic evaluation fixture schema")
    if not 1 <= int(top_k) <= MAX_SEMANTIC_TOP_K:
        raise ValueError(
            f"semantic evaluation top_k must be between 1 and {MAX_SEMANTIC_TOP_K}"
        )
    top_k = int(top_k)
    relevant_labels = RELEVANCE_LABELS[relevance_definition]
    retrievals: dict[int, SemanticRetrievalResult] = {}
    unavailable_queries: list[int] = []
    for query_id in fixture.query_source_article_ids:
        result = retriever.retrieve_source_neighbors(query_id, top_k=top_k)
        retrievals[query_id] = result
        if result.status not in {
            SemanticRetrievalStatus.RETRIEVED,
            SemanticRetrievalStatus.NO_CANDIDATES,
        }:
            unavailable_queries.append(query_id)

    evaluations: list[RelationshipEvaluation] = []
    for relationship in fixture.relationships:
        result = retrievals[relationship.query_source_article_id]
        candidates_by_id = {}
        for rank, candidate in enumerate(result.candidates[:top_k], start=1):
            candidates_by_id.setdefault(
                candidate.candidate_source_article_id,
                (rank, candidate.native_distance),
            )
        rank_distance = candidates_by_id.get(
            relationship.candidate_source_article_id
        )
        evaluations.append(
            RelationshipEvaluation(
                query_source_article_id=relationship.query_source_article_id,
                candidate_source_article_id=relationship.candidate_source_article_id,
                label=relationship.label,
                candidate_rank=rank_distance[0] if rank_distance else None,
                native_distance=rank_distance[1] if rank_distance else None,
            )
        )

    recall_values: list[float] = []
    reciprocal_ranks: list[float] = []
    unavailable_query_ids = set(unavailable_queries)
    for query_id in fixture.query_source_article_ids:
        if query_id in unavailable_query_ids:
            continue
        relevant = [
            item
            for item in evaluations
            if item.query_source_article_id == query_id
            and item.label in relevant_labels
        ]
        if not relevant:
            continue
        found = [item for item in relevant if item.candidate_rank is not None]
        recall_values.append(len(found) / len(relevant))
        first_rank = min(
            (item.candidate_rank for item in found if item.candidate_rank is not None),
            default=None,
        )
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)

    distributions = []
    present_labels = sorted(
        {item.label for item in evaluations},
        key=lambda label: label.value,
    )
    for label in present_labels:
        labeled = [item for item in evaluations if item.label is label]
        distances = [
            item.native_distance
            for item in labeled
            if item.native_distance is not None
        ]
        distributions.append(
            LabelDistanceDistribution(
                label=label,
                labeled_count=len(labeled),
                retrieved_count=len(distances),
                minimum_distance=min(distances) if distances else None,
                maximum_distance=max(distances) if distances else None,
                mean_distance=(sum(distances) / len(distances)) if distances else None,
            )
        )

    evaluable_relationships = [
        item
        for item in evaluations
        if item.query_source_article_id not in unavailable_query_ids
    ]
    found_count = sum(
        item.candidate_rank is not None for item in evaluable_relationships
    )
    return SemanticEvaluationMetrics(
        schema_version=fixture.schema_version,
        relevance_definition=relevance_definition,
        relevant_labels=tuple(sorted(relevant_labels, key=lambda label: label.value)),
        top_k=top_k,
        query_count=len(fixture.query_source_article_ids),
        evaluable_query_count=len(recall_values),
        evaluable_labeled_pair_count=len(evaluable_relationships),
        recall_at_k=(sum(recall_values) / len(recall_values)) if recall_values else 0.0,
        mean_reciprocal_rank=(
            sum(reciprocal_ranks) / len(reciprocal_ranks)
            if reciprocal_ranks
            else 0.0
        ),
        top_k_labeled_coverage=(
            found_count / len(evaluable_relationships)
            if evaluable_relationships
            else 0.0
        ),
        relationships=tuple(evaluations),
        label_distributions=tuple(distributions),
        missing_labeled_pairs=tuple(
            item for item in evaluations if item.candidate_rank is None
        ),
        unavailable_queries=tuple(unavailable_queries),
    )
