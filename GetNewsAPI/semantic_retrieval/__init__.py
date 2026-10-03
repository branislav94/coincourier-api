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
from .shadow import (
    DEFAULT_SEMANTIC_EVIDENCE_VERSION,
    SemanticShadowRunResult,
    run_semantic_shadow,
)

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
    "SemanticShadowRunResult",
    "DEFAULT_SEMANTIC_EVIDENCE_VERSION",
    "evaluate_retrieval",
    "load_evaluation_fixture",
    "run_semantic_shadow",
]
