"""Offline Phase 6C1 semantic retrieval and evaluation foundation."""

from .models import (
    SemanticCandidate,
    SemanticRetrievalResult,
    SemanticRetrievalSettings,
    SemanticRetrievalStatus,
)
from .evaluation import (
    EvaluationFixture,
    LabelDistanceDistribution,
    LabeledRelationship,
    RelationshipEvaluation,
    RelationshipLabel,
    RelevanceDefinition,
    SemanticEvaluationMetrics,
    evaluate_retrieval,
    load_evaluation_fixture,
)
from .service import SemanticRetrievalService

__all__ = [
    "SemanticCandidate",
    "EvaluationFixture",
    "LabelDistanceDistribution",
    "LabeledRelationship",
    "RelationshipEvaluation",
    "RelationshipLabel",
    "RelevanceDefinition",
    "SemanticEvaluationMetrics",
    "SemanticRetrievalResult",
    "SemanticRetrievalService",
    "SemanticRetrievalSettings",
    "SemanticRetrievalStatus",
    "evaluate_retrieval",
    "load_evaluation_fixture",
]
