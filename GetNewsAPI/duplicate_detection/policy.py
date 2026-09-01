"""Explainable deterministic policy for pairwise article relationships."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from .event_matching import EventFacts, extract_event_facts
from .identities import ArticleIdentity, build_article_identity
from .lexical import token_set_jaccard


class AssessmentType(str, Enum):
    EXACT_DUPLICATE = "exact_duplicate"
    SAME_EVENT_DUPLICATE = "same_event_duplicate"
    MATERIAL_UPDATE = "material_update"
    RELATED_EVENT = "related_event"
    BROAD_TOPIC_OVERLAP = "broad_topic_overlap"


@dataclass(frozen=True)
class DuplicateAssessment:
    assessment_type: AssessmentType
    same_provider_article_id: bool
    same_event_id: bool
    same_canonical_url: bool
    same_content_hash: bool
    title_token_jaccard: float
    shared_entities: tuple[str, ...]
    shared_dates: tuple[str, ...]
    shared_numbers: tuple[str, ...]
    publication_distance_hours: float | None
    reason_codes: tuple[str, ...]
    policy_version: str


def _same_nonempty(left: str | None, right: str | None) -> bool:
    return bool(left and right and left == right)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _publication_distance_hours(article: Mapping[str, Any], candidate: Mapping[str, Any]) -> float | None:
    left = _parse_datetime(article.get("publish_date"))
    right = _parse_datetime(candidate.get("publish_date"))
    if left is None or right is None:
        return None
    return abs((left - right).total_seconds()) / 3600


def _new_fact_codes(current: EventFacts, prior: EventFacts) -> tuple[str, ...]:
    codes = []
    if set(current.dates) - set(prior.dates):
        codes.append("new_date")
    if set(current.numbers) - set(prior.numbers):
        codes.append("new_numeric_value")
    if current.key_entities - prior.key_entities:
        codes.append("new_named_participant")
    if set(current.actions) - set(prior.actions):
        codes.append("new_action_or_status")
    return tuple(codes)


def assess_relationship(
    article: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    policy_version: str = "v1",
    lookback_hours: int = 72,
) -> DuplicateAssessment:
    current_identity: ArticleIdentity = build_article_identity(article)
    prior_identity: ArticleIdentity = build_article_identity(candidate)
    current_facts = extract_event_facts(article)
    prior_facts = extract_event_facts(candidate)

    same_provider = _same_nonempty(
        current_identity.provider_article_id, prior_identity.provider_article_id
    )
    same_event = _same_nonempty(current_identity.event_id, prior_identity.event_id)
    same_url = bool(
        current_identity.canonical_url
        and current_identity.canonical_url == prior_identity.canonical_url
    )
    same_content = _same_nonempty(
        current_identity.content_fingerprint, prior_identity.content_fingerprint
    )
    title_similarity = token_set_jaccard(article.get("title"), candidate.get("title"))
    shared_entities = tuple(sorted(set(current_facts.entities) & set(prior_facts.entities)))
    shared_key_entities = tuple(
        item for item in shared_entities if not item.startswith("source:")
    )
    shared_dates = tuple(sorted(set(current_facts.dates) & set(prior_facts.dates)))
    shared_numbers = tuple(sorted(set(current_facts.numbers) & set(prior_facts.numbers)))
    distance = _publication_distance_hours(article, candidate)
    close_in_time = distance is None or distance <= lookback_hours
    new_fact_codes = _new_fact_codes(current_facts, prior_facts)

    if same_provider or same_url or same_content:
        exact_reasons = []
        if same_provider:
            exact_reasons.append("same_provider_article_id")
        if same_url:
            exact_reasons.append("same_canonical_url")
        if same_content:
            exact_reasons.append("same_content_fingerprint")
        classification = AssessmentType.EXACT_DUPLICATE
        reason_codes = tuple(exact_reasons)
    elif same_event:
        if new_fact_codes:
            classification = AssessmentType.MATERIAL_UPDATE
            reason_codes = ("same_event_id", *new_fact_codes)
        else:
            classification = AssessmentType.SAME_EVENT_DUPLICATE
            reason_codes = ("same_event_id", "no_new_structured_facts")
    else:
        inferred_same_event = bool(
            shared_key_entities
            and (shared_dates or shared_numbers)
            and title_similarity >= 0.60
            and close_in_time
        )
        if inferred_same_event and new_fact_codes:
            classification = AssessmentType.MATERIAL_UPDATE
            reason_codes = ("inferred_same_event", *new_fact_codes)
        elif inferred_same_event:
            classification = AssessmentType.SAME_EVENT_DUPLICATE
            reason_codes = ("inferred_same_event", "matching_entity_fact_and_title")
        elif shared_key_entities and (
            any(item.startswith("org:") for item in shared_key_entities)
            or title_similarity >= 0.25
            or shared_dates
            or shared_numbers
        ):
            classification = AssessmentType.RELATED_EVENT
            if set(current_facts.actions) != set(prior_facts.actions):
                reason_codes = ("shared_key_entity", "different_action_or_status")
            elif current_facts.dates and prior_facts.dates and not shared_dates:
                reason_codes = ("shared_key_entity", "different_key_date")
            else:
                reason_codes = ("shared_key_entity", "insufficient_same_event_evidence")
        else:
            classification = AssessmentType.BROAD_TOPIC_OVERLAP
            reason_codes = ("generic_or_no_shared_event_identity",)

    return DuplicateAssessment(
        assessment_type=classification,
        same_provider_article_id=same_provider,
        same_event_id=same_event,
        same_canonical_url=same_url,
        same_content_hash=same_content,
        title_token_jaccard=round(title_similarity, 5),
        shared_entities=shared_entities,
        shared_dates=shared_dates,
        shared_numbers=shared_numbers,
        publication_distance_hours=round(distance, 3) if distance is not None else None,
        reason_codes=reason_codes,
        policy_version=policy_version,
    )
