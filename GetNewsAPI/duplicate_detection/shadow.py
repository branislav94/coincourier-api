"""Orchestration for observational duplicate analysis."""

from __future__ import annotations

import logging
from typing import Mapping

from config import DUPLICATE_LOOKBACK_HOURS, DUPLICATE_POLICY_VERSION
from repositories.duplicate_assessments import DuplicateAssessmentRepository

from .policy import DuplicateAssessment, assess_relationship


def analyze_duplicates_in_shadow(
    article: Mapping[str, Any],
    *,
    repository: DuplicateAssessmentRepository | None = None,
    lookback_hours: int | None = None,
    policy_version: str | None = None,
) -> list[tuple[int, DuplicateAssessment]]:
    """Assess recent relevant pairs and persist evidence without making a decision."""
    article_id = int(article["id"])
    hours = DUPLICATE_LOOKBACK_HOURS if lookback_hours is None else lookback_hours
    version = DUPLICATE_POLICY_VERSION if policy_version is None else policy_version
    assessment_repository = repository or DuplicateAssessmentRepository()
    candidates = assessment_repository.load_candidates(
        article_id,
        lookback_hours=hours,
    )

    results: list[tuple[int, DuplicateAssessment]] = []
    for candidate in sorted(candidates, key=lambda item: int(item["id"])):
        candidate_id = int(candidate["id"])
        if candidate_id == article_id:
            continue
        assessment = assess_relationship(
            article,
            candidate,
            policy_version=version,
            lookback_hours=hours,
        )
        results.append((candidate_id, assessment))
        logging.info(
            "[DUPLICATE-SHADOW] article_id=%s candidate_id=%s classification=%s "
            "same_event_id=%s title_similarity=%.5f reason=%s decision=continue",
            article_id,
            candidate_id,
            assessment.assessment_type.value,
            assessment.same_event_id,
            assessment.title_token_jaccard,
            ",".join(assessment.reason_codes),
        )

    assessment_repository.save_assessments(article_id, results)
    return results
