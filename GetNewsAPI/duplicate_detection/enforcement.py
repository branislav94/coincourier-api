"""Conservative duplicate decisions backed by original source and durable audit."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import config
from repositories.duplicate_assessments import DuplicateAssessmentRepository

from .event_matching import extract_event_facts
from .identities import build_article_identity
from .policy import AssessmentType, DuplicateAssessment, assess_relationship


DUPLICATE_COUNTER_NAMES = (
    "exact_blocks", "same_event_blocks", "material_updates_allowed",
    "related_events_allowed", "broad_topic_overlaps_allowed",
    "semantic_candidates_evaluated", "semantic_candidates_promoted",
    "fail_open_unavailable_embeddings",
    "shadow_would_reject_duplicates", "cross_source_duplicates_confirmed",
)


@dataclass(frozen=True)
class DuplicateDecision:
    reject: bool = False
    mode: str = "shadow"
    stage: str = "raw"
    article_id: int = 0
    assessment: DuplicateAssessment | None = None
    canonical_raw_id: int | None = None
    canonical_rich_id: int | None = None
    canonical_wp_id: int | None = None
    canonical_wp_url: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    available: bool = True
    error: str | None = None
    audit: dict[str, Any] = field(default_factory=dict)


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 and str(value).strip() == str(number) else None


def _semantic_evidence(candidates: Any) -> dict[int, float | None]:
    result: dict[int, float | None] = {}
    for item in tuple(candidates or ())[:200]:
        def value(name: str) -> Any:
            return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        source_id = _positive_id(value("candidate_source_article_id") or value("source_article_id") or value("article_id"))
        if source_id is None:
            continue
        distance = value("native_distance")
        try:
            distance = float(distance)
            if not math.isfinite(distance):
                distance = None
        except (TypeError, ValueError, OverflowError):
            distance = None
        result[source_id] = distance
    return result


def _external(candidate: Mapping[str, Any]) -> bool:
    return bool(
        _positive_id(candidate.get("canonical_wp_id"))
        or _positive_id(candidate.get("canonical_published"))
        or candidate.get("canonical_publish_status") in {"published", "post_created"}
    )


def _unresolved(candidate: Mapping[str, Any]) -> bool:
    return bool(not _external(candidate)
                and _positive_id(candidate.get("canonical_publish_attempt_count"))
                and candidate.get("canonical_publish_status") in {"claimed", "retryable", "manual_review"})


def _credible(candidate: Mapping[str, Any], stage: str) -> bool:
    if not _positive_id(candidate.get("canonical_rich_id")):
        return False
    if candidate.get("processing_status") == "duplicate" or candidate.get("canonical_publish_status") == "duplicate":
        return False
    if stage == "pre_publish":
        return _external(candidate) or _unresolved(candidate)
    return bool(candidate.get("canonical_publish_status") != "manual_review"
                and (_external(candidate) or _positive_id(candidate.get("processed"))))


def _structured_enough(assessment: DuplicateAssessment) -> bool:
    core = tuple(item for item in assessment.shared_entities if not item.startswith("source:"))
    anchors = any(item.startswith(("product:", "protocol:", "products:", "protocols:")) for item in core)
    return bool(core and (assessment.shared_actions or assessment.shared_dates or assessment.shared_numbers or anchors))


def _confirmed(assessment: DuplicateAssessment) -> bool:
    if assessment.new_fact_codes or assessment.evidence_uncertainty or assessment.contradiction_flags:
        return False
    if assessment.assessment_type is AssessmentType.EXACT_DUPLICATE:
        # A mutable URL or provider article ID is not immutable source content.
        return assessment.same_content_hash
    return bool(
        assessment.assessment_type is AssessmentType.SAME_EVENT_DUPLICATE
        and "no_new_structured_facts" in assessment.reason_codes
        and (
            (assessment.same_event_id and _structured_enough(assessment))
            or (
                assessment.event_identity_source == "structured_cross_source"
                and len(assessment.confirmation_anchors) >= 2
            )
        )
    )


def _facts(article: Mapping[str, Any]) -> dict[str, Any]:
    facts = extract_event_facts(article)
    return {
        "entities": facts.entities, "dates": facts.dates,
        "numbers": facts.numbers, "actions": facts.actions,
        "uncertainty": facts.uncertainty,
    }


def assess_duplicate_enforcement(
    article: Mapping[str, Any],
    *,
    stage: str,
    mode: str | None = None,
    repository: DuplicateAssessmentRepository | None = None,
    semantic_candidates: Any = (),
) -> DuplicateDecision:
    """Assess credible source pairs; reject only after the evidence is persisted.

    An unavailable decision is nonblocking at raw processing. The final caller
    may defer publication on application-DB/audit failure instead of creating
    an unaudited post. Vector availability never determines this availability.
    """
    selected_mode = str(mode if mode is not None else getattr(config, "DUPLICATE_ENFORCEMENT_MODE", "shadow")).strip().casefold()
    article_id = _positive_id(article.get("raw_article_id") or article.get("durable_raw_article_id") or article.get("id")) or 0
    counters = dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0)
    base = {"mode": selected_mode, "stage": stage, "article_id": article_id, "counters": counters}
    if selected_mode == "off":
        return DuplicateDecision(**base)
    try:
        if selected_mode not in {"shadow", "enforce"} or stage not in {"raw", "pre_publish"} or not article_id:
            raise ValueError("invalid duplicate assessment context")
        repo = repository or DuplicateAssessmentRepository()
        source = repo.load_source_article(article_id)
        if not source or _positive_id(source.get("id")) != article_id:
            raise ValueError("source article unavailable")
        semantic = _semantic_evidence(semantic_candidates)
        candidates = repo.load_enforcement_candidates(
            article_id,
            lookback_hours=getattr(config, "DUPLICATE_LOOKBACK_HOURS", 72),
            stage=stage,
            source_article_ids=tuple(semantic),
        )
        # Prefer a published canonical, then the earliest credible primary.
        candidates = sorted(candidates, key=lambda row: (
            not _external(row),
            str(row.get("canonical_published_at") or row.get("canonical_post_created_at") or row.get("publish_date") or ""),
            _positive_id(row.get("id")) or 0,
            _positive_id(row.get("canonical_rich_id")) or 0,
        ))
        assessments: list[tuple[int, DuplicateAssessment]] = []
        candidate_audits: dict[int, dict[str, Any]] = {}
        chosen: tuple[Mapping[str, Any], DuplicateAssessment] | None = None
        unresolved: tuple[Mapping[str, Any], DuplicateAssessment] | None = None
        seen: set[int] = set()
        for candidate in candidates:
            candidate_id = _positive_id(candidate.get("id"))
            if not candidate_id or candidate_id == article_id or candidate_id in seen or not _credible(candidate, stage):
                continue
            seen.add(candidate_id)
            assessment = assess_relationship(source, candidate, policy_version=getattr(config, "DUPLICATE_POLICY_VERSION", "v1"), lookback_hours=getattr(config, "DUPLICATE_LOOKBACK_HOURS", 72))
            confirmed = _confirmed(assessment)
            if confirmed and assessment.event_identity_source == "structured_cross_source":
                counters["cross_source_duplicates_confirmed"] += 1
            assessments.append((candidate_id, assessment))
            if candidate_id in semantic:
                counters["semantic_candidates_evaluated"] += 1
                counters["semantic_candidates_promoted"] += int(confirmed)
            allowed_counter = {
                AssessmentType.MATERIAL_UPDATE: "material_updates_allowed",
                AssessmentType.RELATED_EVENT: "related_events_allowed",
                AssessmentType.BROAD_TOPIC_OVERLAP: "broad_topic_overlaps_allowed",
            }.get(assessment.assessment_type)
            if allowed_counter:
                counters[allowed_counter] += 1
            if confirmed:
                if stage == "pre_publish" and _unresolved(candidate):
                    if unresolved is None:
                        unresolved = candidate, assessment
                elif chosen is None:
                    chosen = candidate, assessment
            candidate_audits[candidate_id] = {
                "classification": assessment.assessment_type.value,
                "same_event_id": assessment.same_event_id,
                "event_identity_source": assessment.event_identity_source,
                "provider_article_id": build_article_identity(source).provider_article_id,
                "event_id": build_article_identity(source).event_id,
                "prior_provider_article_id": build_article_identity(candidate).provider_article_id,
                "prior_event_id": build_article_identity(candidate).event_id,
                "canonical_raw_id": candidate_id,
                "canonical_rich_id": _positive_id(candidate.get("canonical_rich_id")),
                "canonical_wp_id": _positive_id(candidate.get("canonical_wp_id")),
                "canonical_wp_url": candidate.get("canonical_wp_url"),
                "semantic_distance": semantic.get(candidate_id),
                "title_similarity": assessment.title_token_jaccard,
                "shared_entities": assessment.shared_entities,
                "new_fact_codes": assessment.new_fact_codes,
                "new_fact_flags": assessment.new_fact_codes,
                "contradiction_flags": assessment.contradiction_flags,
                "confirmation_anchors": assessment.confirmation_anchors,
                "shared_actions": assessment.shared_actions,
                "evidence_uncertainty": assessment.evidence_uncertainty,
                "decision_reason": "insufficient_structured_evidence" if assessment.same_event_id and assessment.assessment_type is AssessmentType.SAME_EVENT_DUPLICATE and not _structured_enough(assessment) else ",".join(assessment.reason_codes),
                "current_facts": _facts(source), "prior_facts": _facts(candidate),
                "decision": (
                    "would_reject_duplicate" if confirmed and not (stage == "pre_publish" and _unresolved(candidate)) else "would_continue"
                ) if selected_mode == "shadow" else (
                    ("defer_manual_review" if stage == "pre_publish" and _unresolved(candidate) else "reject_duplicate") if confirmed else "continue"
                ),
                "confirmed_duplicate": confirmed,
            }
        reject = chosen is not None and selected_mode == "enforce"
        defer = not chosen and unresolved is not None and selected_mode == "enforce"
        if reject:
            counters["exact_blocks" if chosen[1].assessment_type is AssessmentType.EXACT_DUPLICATE else "same_event_blocks"] += 1
        if selected_mode == "shadow" and chosen is not None:
            counters["shadow_would_reject_duplicates"] = 1
        decision = (
            "would_reject_duplicate" if chosen is not None else "would_continue"
        ) if selected_mode == "shadow" else (
            "reject_duplicate" if reject else "defer_manual_review" if defer else "continue"
        )
        audit = {"stage": stage, "mode": selected_mode, "decision": decision, "counters": counters, "candidate_audits": candidate_audits}
        # No silent terminal rejection if the evidence could not be stored.
        persisted = repo.save_assessments(article_id, assessments, audit_context=audit)
        if persisted != len(assessments):
            raise RuntimeError("incomplete duplicate assessment audit")
        for candidate_id, assessment in assessments:
            evidence = candidate_audits[candidate_id]
            logging.info("[DUPLICATE-POLICY] article_id=%s candidate_id=%s classification=%s same_event_id=%s event_identity_source=%s confirmed_duplicate=%s new_fact_flags=%s contradiction_flags=%s title_similarity=%.5f evidence=%s reason=%s mode=%s stage=%s decision=%s canonical_wp_id=%s",
                         article_id, candidate_id, assessment.assessment_type.value, assessment.same_event_id,
                         assessment.event_identity_source, str(evidence["confirmed_duplicate"]).lower(),
                         ",".join(assessment.new_fact_codes) or "none", ",".join(assessment.contradiction_flags) or "none", assessment.title_token_jaccard,
                         json.dumps(evidence, ensure_ascii=True, separators=(",", ":"), default=str), evidence["decision_reason"], selected_mode, stage, evidence["decision"], evidence["canonical_wp_id"])
        logging.info("[DUPLICATE-COUNTERS] article_id=%s stage=%s mode=%s counters=%s", article_id, stage, selected_mode, json.dumps(counters, sort_keys=True))
        match = chosen or unresolved
        if match:
            canonical, assessment = match
            return DuplicateDecision(**base, reject=reject, assessment=assessment, canonical_raw_id=int(canonical["id"]), canonical_rich_id=_positive_id(canonical.get("canonical_rich_id")), canonical_wp_id=_positive_id(canonical.get("canonical_wp_id")), canonical_wp_url=canonical.get("canonical_wp_url"), audit=audit, available=not defer, error="prior_publication_unresolved" if defer else None)
        return DuplicateDecision(**base, audit=audit)
    except Exception as error:
        # Exception messages can contain credentials, SQL, or source text.
        safe_error = type(error).__name__
        for name in ("exact_blocks", "same_event_blocks", "shadow_would_reject_duplicates"):
            counters[name] = 0
        logging.warning("[DUPLICATE-POLICY] article_id=%s mode=%s stage=%s decision=%s available=false error_type=%s", article_id, selected_mode, stage, "would_continue" if selected_mode == "shadow" else "continue", safe_error)
        return DuplicateDecision(**base, available=False, error=safe_error)
