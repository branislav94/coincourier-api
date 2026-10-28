"""CMS-neutral orchestration for durable publication attempts."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Callable, Mapping

from config import DUPLICATE_ENFORCEMENT_MODE, PIPELINE_FRESH_START_AFTER_UTC_SQL, PUBLISH_CLAIM_TIMEOUT_MINUTES
from duplicate_detection.enforcement import DUPLICATE_COUNTER_NAMES, DuplicateDecision
from duplicate_detection.publication import (
    PublicationDuplicateGuard, PublicationDuplicateRejected, PublicationDuplicateUnavailable,
)
from repositories.publication import PublicationClaim, PublicationRepository
from repositories.state import claim_prefix, safe_error_message

from .base import Publisher
from .models import (
    PublicationArticle, PublicationContext, PublicationImage, PublicationResult,
    is_fresh_publication_state,
)


logger = logging.getLogger(__name__)
ImagePreparer = Callable[[Mapping[str, Any]], tuple[int | None, dict[str, Any]]]


def build_publication_key(
    rich_article_id: int,
    raw_article_id: int | None,
    source_url: str | None,
) -> str:
    """Build stable identity from durable IDs, with an orphan-only URL fallback."""
    if raw_article_id is not None:
        return f"coincourier:{rich_article_id}:{raw_article_id}"
    normalized_url = (source_url or "").strip().lower()
    source_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return f"coincourier:rich:{rich_article_id}:source:{source_hash}"


def publication_key_prefix(publication_key: str) -> str:
    return hashlib.sha256(publication_key.encode("utf-8")).hexdigest()[:12]


class PublishingService:
    """Claim, reconcile, invoke one publisher, and complete local state."""

    def __init__(
        self,
        repository: PublicationRepository,
        publisher: Publisher,
        image_preparer: ImagePreparer,
        *,
        claim_timeout_minutes: int = PUBLISH_CLAIM_TIMEOUT_MINUTES,
        fresh_start_after: str | None = PIPELINE_FRESH_START_AFTER_UTC_SQL,
        duplicate_mode: str | None = None,
        duplicate_guard_factory: Callable[[int | None], PublicationDuplicateGuard] | None = None,
    ) -> None:
        self.repository = repository
        self.publisher = publisher
        self.image_preparer = image_preparer
        self.claim_timeout_minutes = claim_timeout_minutes
        self.fresh_start_after = fresh_start_after
        self.duplicate_mode = str(DUPLICATE_ENFORCEMENT_MODE if duplicate_mode is None else duplicate_mode).strip().lower()
        if self.duplicate_mode not in {"off", "shadow", "enforce"}:
            raise ValueError("invalid duplicate enforcement mode")
        self.duplicate_guard_factory = duplicate_guard_factory

    def publish_due(self, limit: int) -> dict[str, int]:
        if self.duplicate_mode == "off" or limit <= 0:
            return self._publish_due_locked(limit)
        guard = (self.duplicate_guard_factory or (
            lambda raw_id: PublicationDuplicateGuard(raw_id, mode=self.duplicate_mode)
        ))(None)
        # Enforcement claims while holding the cluster mutex. Two fresh claims
        # could mistake one another for an unresolved external CREATE attempt.
        try:
            with guard.hold():
                return self._publish_due_locked(limit, duplicate_guard=guard)
        except PublicationDuplicateUnavailable as exc:
            logger.warning("[DUPLICATE-PUBLISH] stage=pre_publish decision=defer code=%s", exc.code)
            return {
                "attempted": 0, "succeeded": 0, "failed": 0,
                "duplicates_rejected": 0, "duplicate_guard_deferred": 1,
                **dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0),
            }

    def _publish_due_locked(self, limit: int, *, duplicate_guard: PublicationDuplicateGuard | None = None) -> dict[str, int]:
        result = {"attempted": 0, "succeeded": 0, "failed": 0}
        if self.duplicate_mode != "off":
            result.update(duplicates_rejected=0, duplicate_guard_deferred=0)
            result.update(dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0))
        attempted_rich_ids: set[int] = set()
        for _ in range(max(0, limit)):
            claim = self.repository.claim_next(
                timeout_minutes=self.claim_timeout_minutes,
                fresh_start_after=self.fresh_start_after,
                exclude_rich_article_ids=tuple(sorted(attempted_rich_ids)),
            )
            if claim is None:
                break
            # A failed claim can become retryable immediately; reserve it for a later invocation.
            attempted_rich_ids.add(int(claim.article["id"]))
            result["attempted"] += 1
            if duplicate_guard is not None:
                duplicate_guard.decision = None
                raw_id = claim.article.get("raw_article_id") or claim.article.get("durable_raw_article_id")
                duplicate_guard.raw_article_id = _optional_int(raw_id)
            outcome = self._publish_claim(claim, duplicate_guard=duplicate_guard)
            if duplicate_guard is not None and duplicate_guard.decision is not None:
                for name, count in duplicate_guard.decision.counters.items():
                    if name in DUPLICATE_COUNTER_NAMES:
                        result[name] += count
            if isinstance(outcome, DuplicateDecision):
                result["duplicates_rejected" if outcome.reject else "duplicate_guard_deferred"] += 1
            elif outcome:
                result["succeeded"] += 1
            else:
                result["failed"] += 1
        return result

    def _publish_claim(self, claim: PublicationClaim, *, duplicate_guard: PublicationDuplicateGuard | None = None) -> bool | DuplicateDecision:
        row = claim.article
        rich_id = int(row["id"])
        raw_id_value = row.get("raw_article_id") or row.get("durable_raw_article_id")
        raw_id = int(raw_id_value) if raw_id_value is not None else None
        key = row.get("publication_key") or build_publication_key(
            rich_id,
            raw_id,
            row.get("news_url"),
        )
        logger.info(
            "[PUBLISH-CLAIM] rich_article_id=%s attempt=%s claim=%s decision=%s",
            rich_id,
            claim.attempt,
            claim_prefix(claim.token),
            "reclaimed-expired" if claim.recovered else "claimed",
        )

        context = None
        try:
            if raw_id is None:
                raise ValueError("durable raw article identity is unavailable")
            if not self.repository.save_identity(
                rich_id, claim.token, raw_id, key,
                expected_post_id=_optional_int(row.get('wp_post_id')),
                expected_post_url=row.get('wp_post_url'),
            ):
                raise RuntimeError("publication claim ownership was lost while saving identity")

            def persist_external(post_id: int, post_url: str | None) -> None:
                if not self.repository.save_external_post(
                    rich_id,
                    claim.token,
                    post_id,
                    post_url,
                ):
                    raise RuntimeError("publication claim ownership was lost while saving post ID")

            context = PublicationContext(
                published_at_utc=datetime.now(timezone.utc),
                publication_key=key,
                raw_article_id=raw_id,
                rich_article_id=rich_id,
                source_url=row.get("news_url"),
                existing_external_id=_optional_int(row.get("wp_post_id")),
                persist_external_state=persist_external,
                existing_external_url=row.get('wp_post_url'),
                creation_is_fresh=(
                    claim.attempt == 1 and not claim.recovered
                    and is_fresh_publication_state(row)
                ),
            )
            article = PublicationArticle.from_mapping(row)

            reconciled = self.publisher.reconcile(article, context)
            if reconciled is not None:
                return self._complete_result(claim, reconciled)

            if duplicate_guard is not None:
                duplicate_guard.check()
                context = replace(context, pre_create_duplicate_guard=duplicate_guard.check)
            image = self._prepare_image(claim, key)
            publish_result = self.publisher.publish(article, image, context)
            return self._complete_result(claim, publish_result)
        except PublicationDuplicateRejected as exc:
            try:
                if raw_id is None or not self.repository.reject_duplicate(rich_id, raw_id, exc.decision, token=claim.token):
                    raise RuntimeError("duplicate disposition ownership was lost")
                logger.info("[DUPLICATE-PUBLISH] rich_article_id=%s raw_article_id=%s stage=pre_publish decision=reject_duplicate canonical_wp_id=%s",
                            rich_id, raw_id, exc.decision.canonical_wp_id)
                return exc.decision
            except Exception as error:
                self.repository.fail(rich_id, claim.token, safe_error_message(error, "duplicate disposition"))
                return False
        except PublicationDuplicateUnavailable as exc:
            if exc.code == "prior_publication_unresolved":
                saved = self.repository.require_manual_reconciliation(rich_id, claim.token, "duplicate_prior_publication_unresolved")
            else:
                saved = self.repository.defer_duplicate_guard(
                    rich_id, claim.token, creation_is_fresh=bool(context and context.creation_is_fresh),
                )
            if not saved:
                self.repository.fail(rich_id, claim.token, "duplicate guard deferral ownership was lost")
                return False
            logger.warning("[DUPLICATE-PUBLISH] rich_article_id=%s stage=pre_publish decision=defer code=%s", rich_id, exc.code)
            return exc.decision or DuplicateDecision(mode="enforce", stage="pre_publish", article_id=raw_id or 0, available=False, error=exc.code)
        except Exception as exc:
            logger.exception(
                "[WP-PUBLISH] rich_article_id=%s state=retryable error_type=%s",
                rich_id,
                type(exc).__name__,
            )
            self.repository.fail(
                rich_id,
                claim.token,
                safe_error_message(exc, "publication"),
            )
            return False
        except BaseException:
            try:
                self.repository.release_interrupted(rich_id, claim.token)
            except Exception:
                logger.exception(
                    "[PUBLISH-CLAIM] rich_article_id=%s claim=%s decision=release-failed",
                    rich_id,
                    claim_prefix(claim.token),
                )
            raise

    def _prepare_image(
        self,
        claim: PublicationClaim,
        publication_key: str,
    ) -> PublicationImage | None:
        row = claim.article
        rich_id = int(row["id"])
        media_id = _optional_int(row.get("wp_media_id"))
        metadata = _metadata_from_row(row)

        if media_id is not None and self.publisher.external_media_exists(media_id):
            logger.info(
                "[WP-PUBLISH] rich_article_id=%s wp_media_id=%s state=reuse-media-local",
                rich_id,
                media_id,
            )
            return PublicationImage(external_id=media_id, metadata=metadata)
        if media_id is not None:
            if not self.repository.clear_media(rich_id, claim.token):
                raise RuntimeError("publication claim ownership was lost while clearing stale media")
            media_id = None
            metadata = {}

        recovered_media_id = self.publisher.find_external_media(publication_key)
        if recovered_media_id is not None:
            if not self.repository.save_media(
                rich_id,
                claim.token,
                recovered_media_id,
                metadata,
            ):
                raise RuntimeError("publication claim ownership was lost while recovering media")
            logger.info(
                "[WP-PUBLISH] rich_article_id=%s wp_media_id=%s state=reuse-media-wp-meta",
                rich_id,
                recovered_media_id,
            )
            return PublicationImage(external_id=recovered_media_id, metadata=metadata)

        uploaded_id, uploaded_metadata = self.image_preparer(row)
        if uploaded_id is None:
            return None

        external_error: Exception | None = None
        try:
            self.publisher.persist_external_media_identity(uploaded_id, publication_key)
        except Exception as exc:
            external_error = exc
        local_saved = self.repository.save_media(
            rich_id,
            claim.token,
            uploaded_id,
            uploaded_metadata,
        )
        if not local_saved:
            raise RuntimeError("uploaded media exists but local media state could not be saved")
        if external_error is not None:
            raise external_error
        return PublicationImage(external_id=uploaded_id, metadata=uploaded_metadata)

    def _complete_result(
        self,
        claim: PublicationClaim,
        result: PublicationResult,
    ) -> bool:
        rich_id = int(claim.article["id"])
        if not result.success:
            if result.error == 'post_identity_manual_reconciliation_required':
                if not self.repository.require_manual_reconciliation(
                    rich_id, claim.token, result.error,
                ):
                    raise RuntimeError('publication ownership was lost while deferring reconciliation')
                logger.error(
                    '[WP-PUBLISH] rich_article_id=%s state=manual_review code=%s',
                    rich_id, result.error,
                )
                return False
            error = RuntimeError(result.error or "publisher returned an unsuccessful result")
            self.repository.fail(
                rich_id,
                claim.token,
                safe_error_message(error, "publication"),
            )
            return False
        if not self.repository.complete(rich_id, claim.token):
            raise RuntimeError("publication claim ownership was lost before completion")
        logger.info(
            "[WP-PUBLISH] rich_article_id=%s wp_post_id=%s wp_media_id=%s state=published",
            rich_id,
            result.external_id,
            result.media_external_id or claim.article.get("wp_media_id"),
        )
        return True


def _optional_int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _metadata_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("wp_media_metadata_json")
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
