"""Bounded rescue of observed stock candidates; legal and byte safety stay hard."""
from __future__ import annotations

import ipaddress
import logging
import math
import re
from typing import Callable
from urllib.parse import urlsplit

from config import (
    IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED,
    IMAGE_EMERGENCY_STOCK_RESCUE_LIMIT,
    IMAGE_MIN_HEIGHT,
    IMAGE_MIN_WIDTH,
)

from .downloader import ImageDownloadError, ImageDownloader, MAX_IMAGE_BYTES
from .license_policy import ImageLicensePolicy
from .models import (
    DownloadedImage, ImageCandidate, ImageSearchResult, RescueValidationResult,
    StockRescueCandidate, StockRescueContext,
)
from .reuse import (
    candidate_identity_values, candidate_recent_local_usage, candidate_recent_wp_usage,
    downloaded_recent_local_usage,
)
from .scoring import rank_candidates


logger = logging.getLogger(__name__)
SOFT_REASONS = frozenset({
    "score_below_threshold", "local_history_identity_reuse", "wordpress_history_reuse",
    "local_history_sha_reuse", "local_history_dhash_reuse",
})
VALIDATION_REASONS = frozenset({
    "conversion_failed", "attribution_persistence_failed", "attribution_missing",
    "attribution_too_large", "metadata_update_failed", "media_metadata_failed",
    "media_upload_failed", "license_rejected", "required_fields", "invalid_original_url",
    "unsupported_format", "invalid_size", "dimensions_rejected", "orientation_rejected",
})
# Same existing fail-closed publication bound; no attribution is truncated.
MAX_REQUIRED_ATTRIBUTION_CHARS = 16_384
CC_BY_CHANGE_NOTICE = "\nChanges: cropped and resized for publication."
SUPPORTED_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


def transport_key(candidate: ImageCandidate) -> tuple[str, str]:
    return candidate.provider, candidate.usable_url


def validation_outcome(value: bool | RescueValidationResult) -> tuple[bool, str]:
    if isinstance(value, RescueValidationResult):
        if value.accepted:
            return True, ""
        return False, value.reason if value.reason in VALIDATION_REASONS else "validation_failed"
    return (True, "") if value is True else (False, "conversion_failed")


def _public_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").rstrip(".").lower()
        if (
            parts.scheme not in {"http", "https"} or not host
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 80, 443}
            or any(character.isspace() or ord(character) < 32 for character in value)
            or re.search(r"%(?![0-9a-fA-F]{2})", parts.path + parts.query)
            or host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal"))
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            if all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in host.split(".")):
                return False
            return "." in host and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
    except (ValueError, TypeError, UnicodeError):
        return False


def _hard_safety_reason(candidate: ImageCandidate, policy: ImageLicensePolicy) -> str:
    license_valid, _reason = policy.validate(candidate)
    if not license_valid:
        return "license_rejected"
    if candidate.provider not in {"pexels", "pixabay"}:
        from .providers.wikimedia import _parse_license
        label = {
            "cc0": "CC0", "pdm": "PDM", "cc-by": f"CC BY {candidate.license_version}",
        }.get(candidate.license_name, "")
        if not _parse_license(label, candidate.license_url):
            return "license_rejected"
    if not candidate.asset_key or not candidate.usable_url or not candidate.source_page_url:
        return "required_fields"
    if not candidate.attribution_text.strip():
        return "attribution_missing"
    notice = CC_BY_CHANGE_NOTICE if candidate.license_name == "cc-by" else ""
    if len(candidate.attribution_text) + len(notice) > MAX_REQUIRED_ATTRIBUTION_CHARS:
        return "attribution_too_large"
    if not _public_http_url(candidate.usable_url) or not _public_http_url(candidate.source_page_url):
        return "invalid_original_url"
    image_host = (urlsplit(candidate.usable_url).hostname or "").lower()
    source_host = (urlsplit(candidate.source_page_url).hostname or "").lower()
    if candidate.provider == "pexels" and (
        image_host != "images.pexels.com" or source_host not in {"pexels.com", "www.pexels.com"}
    ):
        return "invalid_original_url"
    if candidate.provider == "pixabay" and (
        image_host not in {"pixabay.com", "cdn.pixabay.com"}
        or source_host not in {"pixabay.com", "www.pixabay.com"}
    ):
        return "invalid_original_url"
    if candidate.provider == "wikimedia":
        from .commons_identity import COMMONS_PAGE_HOSTS, commons_identities, commons_identity_conflict
        from .providers.wikimedia import _valid_original_url
        if not _valid_original_url(candidate.usable_url) or source_host not in COMMONS_PAGE_HOSTS:
            return "invalid_original_url"
        identities = candidate_identity_values(candidate)
        if not commons_identities(candidate.source_page_url):
            return "unrecognized_commons_identity"
        if commons_identity_conflict(*identities):
            return "commons_identity_conflict"
    if candidate.mime_type and candidate.mime_type.lower() not in SUPPORTED_MIMES:
        return "unsupported_format"
    size = candidate.metadata.get("size") or candidate.metadata.get("original_size")
    if size is not None:
        try:
            if not 0 < int(size) <= MAX_IMAGE_BYTES:
                return "invalid_size"
        except (ValueError, TypeError, OverflowError):
            return "invalid_size"
    if candidate.width is not None and candidate.width < IMAGE_MIN_WIDTH:
        return "dimensions_rejected"
    if candidate.height is not None and candidate.height < IMAGE_MIN_HEIGHT:
        return "dimensions_rejected"
    if candidate.width and candidate.height and candidate.width < candidate.height:
        return "orientation_rejected"
    if not math.isfinite(candidate.final_score) or not math.isfinite(candidate.provider_threshold):
        return "required_fields"
    return ""


def create_rescue_context(
    candidates: list[ImageCandidate], *, provider_precedence: tuple[str, ...],
    downloader: ImageDownloader, license_policy: ImageLicensePolicy,
    local_usage: list[dict], recent_wp_usage: list[dict], article_correlation_id: str,
) -> StockRescueContext:
    """Check every observed candidate independently of its relevance score."""
    context = StockRescueContext(
        provider_precedence=provider_precedence, downloader=downloader,
        license_policy=license_policy, article_correlation_id=article_correlation_id,
        local_usage=list(local_usage),
    )
    parents = list(range(len(candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owner: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        for identity in candidate_identity_values(candidate):
            other = owner.setdefault(identity, index)
            left, right = find(index), find(other)
            parents[max(left, right)] = min(left, right)
    group_reuse: dict[int, str] = {}
    for index, candidate in enumerate(candidates):
        root = find(index)
        context.candidate_groups[id(candidate)] = root
        if candidate_recent_local_usage(candidate, local_usage):
            group_reuse[root] = "local_history_identity_reuse"
        elif candidate_recent_wp_usage(candidate, recent_wp_usage) and root not in group_reuse:
            group_reuse[root] = "wordpress_history_reuse"
    for index, candidate in enumerate(candidates):
        try:
            hard_reason = _hard_safety_reason(candidate, license_policy)
        except Exception:
            hard_reason = "required_fields"
        if hard_reason:
            logger.info(
                "[IMG-RESCUE] excluded article=%s provider=%s asset_key=%s reason=%s",
                article_correlation_id, candidate.provider, candidate.asset_key, hard_reason,
            )
            continue
        reasons: list[str] = []
        if candidate.final_score < candidate.provider_threshold:
            reasons.append("score_below_threshold")
        root = find(index)
        if root in group_reuse:
            reasons.append(group_reuse[root])
        context.entries.append(StockRescueCandidate(candidate, tuple(reasons), group_index=root))
    return context


def remember_download(context: StockRescueContext, candidate: ImageCandidate, downloaded: DownloadedImage) -> None:
    for entry in context.entries:
        if entry.candidate.usable_url == candidate.usable_url:
            entry.downloaded = downloaded


def mark_soft_rejection(context: StockRescueContext, candidate: ImageCandidate, reason: str, *, group: bool = False) -> None:
    if reason not in SOFT_REASONS:
        return
    matching_groups = {entry.group_index for entry in context.entries if entry.candidate is candidate}
    if id(candidate) in context.candidate_groups:
        matching_groups.add(context.candidate_groups[id(candidate)])
    for entry in context.entries:
        if entry.candidate is candidate or (group and entry.group_index in matching_groups):
            entry.soft_reasons = tuple(dict.fromkeys((*entry.soft_reasons, reason)))


def hard_fail_transport(context: StockRescueContext, candidate: ImageCandidate, reason: str) -> None:
    key = transport_key(candidate)
    context.failed_transports[key] = reason
    for entry in context.entries:
        # Strict selection still treats providers as delivery variants. Rescue
        # must never repeat a URL already proven unusable by any provider.
        if entry.candidate.usable_url == candidate.usable_url:
            entry.hard_reason = reason
            entry.downloaded = None


def hard_fail_group(context: StockRescueContext, candidate: ImageCandidate, reason: str) -> None:
    matching_groups = {entry.group_index for entry in context.entries if entry.candidate is candidate}
    if id(candidate) in context.candidate_groups:
        matching_groups.add(context.candidate_groups[id(candidate)])
    for entry in context.entries:
        if entry.group_index in matching_groups:
            hard_fail_transport(context, entry.candidate, reason)


def reject_rescue_candidate(result: ImageSearchResult, candidate: ImageCandidate, reason: str) -> None:
    if result.rescue_context is not None:
        hard_fail_transport(result.rescue_context, candidate, reason if reason in VALIDATION_REASONS else "validation_failed")
    if result.candidate is candidate:
        if result.rescue_context is not None:
            result.rescue_context.selected_provider = ""
            result.rescue_context.original_rejection_reason = ""
        result.candidate, result.downloaded = None, None


def _tier(entry: StockRescueCandidate) -> int:
    reasons = set(entry.soft_reasons)
    if not reasons:
        return 0
    if reasons & {"local_history_sha_reuse", "local_history_dhash_reuse"}:
        return 4
    recent = bool(reasons & {"local_history_identity_reuse", "wordpress_history_reuse"})
    below = "score_below_threshold" in reasons
    return 3 if recent and below else 2 if recent else 1


def _download_safety_reason(downloaded: DownloadedImage) -> str:
    if not downloaded.content or len(downloaded.content) > MAX_IMAGE_BYTES:
        return "invalid_size"
    if downloaded.mime_type not in SUPPORTED_MIMES:
        return "unsupported_format"
    if downloaded.width < IMAGE_MIN_WIDTH or downloaded.height < IMAGE_MIN_HEIGHT:
        return "dimensions_rejected"
    if downloaded.width < downloaded.height:
        return "orientation_rejected"
    return ""


def emergency_summary(context: StockRescueContext | None) -> dict:
    return {
        "emergency_stock_rescue_attempted": bool(context and context.attempted),
        "emergency_candidates_available": context.candidates_available if context else 0,
        "emergency_download_attempts": context.download_attempts if context else 0,
        "emergency_attempts": context.candidate_attempts if context else 0,
        "emergency_selected": bool(context and context.selected_provider),
        "emergency_selected_provider": context.selected_provider if context else "",
        "emergency_original_rejection_reason": context.original_rejection_reason if context else "",
        "emergency_completed": bool(context and context.completed),
    }


def _ranked_pool(context: StockRescueContext) -> list[StockRescueCandidate]:
    provider_order = {provider: index for index, provider in enumerate(context.provider_precedence)}
    eligible = [
        entry for entry in context.entries
        if not entry.hard_reason and (
            entry.soft_reasons or (
                entry.candidate.final_score >= entry.candidate.provider_threshold
                and transport_key(entry.candidate) not in context.strict_attempted_transports
            )
        )
        and not any(key[1] == entry.candidate.usable_url for key in context.failed_transports)
        and transport_key(entry.candidate) not in context.processed_transports
    ]
    ranked = rank_candidates([entry.candidate for entry in eligible])
    tie_order = {id(candidate): index for index, candidate in enumerate(ranked)}
    eligible.sort(key=lambda entry: (
        _tier(entry), -entry.candidate.final_score,
        provider_order.get(entry.candidate.provider, len(provider_order)),
        tie_order[id(entry.candidate)],
    ))
    unique: list[StockRescueCandidate] = []
    seen: set[tuple[str, str]] = set()
    for entry in eligible:
        if transport_key(entry.candidate) not in seen:
            unique.append(entry)
            seen.add(transport_key(entry.candidate))
    return unique


def rescue_images(
    strict_result: ImageSearchResult, *, downloader: ImageDownloader | None = None,
    post_download_validator: Callable[[ImageCandidate, DownloadedImage], bool | RescueValidationResult] | None = None,
    limit: int = IMAGE_EMERGENCY_STOCK_RESCUE_LIMIT,
    enabled: bool = IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED,
) -> ImageSearchResult:
    """Rescue observed unattempted or soft-rejected stock without a new search."""
    context = strict_result.rescue_context
    if strict_result.candidate is not None:
        return strict_result
    if context is None:
        summary = emergency_summary(None)
        summary.update(emergency_completed=True, emergency_stock_rescue_attempted=bool(enabled))
        return ImageSearchResult(
            providers_attempted=strict_result.providers_attempted,
            provider_failures=dict(strict_result.provider_failures), emergency_summary=summary,
            article_correlation_id=strict_result.article_correlation_id,
        )
    context.attempted = bool(enabled)
    if not enabled:
        context.completed = True
        return ImageSearchResult(rescue_context=context, emergency_summary=emergency_summary(context))
    context.candidates_available = max(context.candidates_available, len(_ranked_pool(context)))
    image_downloader = downloader or context.downloader or ImageDownloader()
    selected, selected_download = None, None
    while True:
        pending = _ranked_pool(context)
        if not pending or context.candidate_attempts >= max(0, limit):
            break
        entry = pending[0]
        candidate = entry.candidate
        key = transport_key(candidate)
        try:
            reason = _hard_safety_reason(candidate, context.license_policy)
        except Exception:
            reason = "required_fields"
        if reason:
            hard_fail_transport(context, candidate, reason)
            continue
        context.processed_transports.add(key)
        context.candidate_attempts += 1
        original_reason = ",".join(entry.soft_reasons) or "strict_unattempted"
        candidate.metadata.update(
            emergency_stock_rescue=True, emergency_original_rejection_reason=original_reason,
            emergency_rescue_tier=_tier(entry), original_score=candidate.final_score,
            original_threshold=candidate.provider_threshold, original_rejection=original_reason,
        )
        downloaded = entry.downloaded
        if downloaded is None:
            context.download_attempts += 1
            logger.info(
                "[IMG-RESCUE] download article=%s provider=%s asset_key=%s attempt=%s tier=%s original_rejection=%s",
                context.article_correlation_id, candidate.provider, candidate.asset_key,
                context.download_attempts, _tier(entry), original_reason,
            )
            try:
                downloaded = image_downloader.download(candidate)
            except Exception:
                hard_fail_transport(context, candidate, "download_failed")
                logger.info(
                    "[IMG-RESCUE] rejected article=%s provider=%s asset_key=%s reason=download_failed",
                    context.article_correlation_id, candidate.provider, candidate.asset_key,
                )
                continue
            remember_download(context, candidate, downloaded)
        try:
            reason = _download_safety_reason(downloaded)
        except Exception:
            reason = "invalid_size"
        if reason:
            hard_fail_transport(context, candidate, reason)
            continue
        recent_content = downloaded_recent_local_usage(downloaded, context.local_usage)
        if recent_content and not set(entry.soft_reasons) & {"local_history_sha_reuse", "local_history_dhash_reuse"}:
            reason = (
                "local_history_sha_reuse"
                if recent_content.get("content_sha256") == downloaded.content_sha256
                else "local_history_dhash_reuse"
            )
            mark_soft_rejection(context, candidate, reason, group=True)
            original_reason = ",".join(entry.soft_reasons)
            candidate.metadata.update(
                emergency_rescue_tier=4, original_rejection=original_reason,
                emergency_original_rejection_reason=original_reason,
            )
            context.processed_transports.discard(key)
            reranked = _ranked_pool(context)
            if reranked and reranked[0] is not entry:
                logger.info(
                    "[IMG-RESCUE] deferred article=%s provider=%s asset_key=%s reason=%s tier=4",
                    context.article_correlation_id, candidate.provider, candidate.asset_key, reason,
                )
                # Cache bytes and yield to any higher-ranked rescue candidate.
                # A later cached conversion pass remains bounded by the budget.
                continue
            # No candidate outranks this newly demoted item. Complete conversion
            # in this attempt, including when it is the sole item at limit=1.
            context.processed_transports.add(key)
        if post_download_validator is not None:
            try:
                accepted, reason = validation_outcome(post_download_validator(candidate, downloaded))
            except Exception:
                accepted, reason = False, "conversion_failed"
            if not accepted:
                hard_fail_transport(context, candidate, reason)
                logger.info(
                    "[IMG-RESCUE] rejected article=%s provider=%s asset_key=%s reason=%s",
                    context.article_correlation_id, candidate.provider, candidate.asset_key, reason,
                )
                continue
        selected, selected_download = candidate, downloaded
        context.selected_provider = candidate.provider
        context.original_rejection_reason = original_reason
        break
    context.completed = selected is not None or not _ranked_pool(context) or context.candidate_attempts >= max(0, limit)
    summary = emergency_summary(context)
    logger.info(
        "[IMG-RESCUE] terminal article=%s reason=%s selected_provider=%s selected_asset_key=%s "
        "emergency_stock_rescue=%s original_rejection=%s counters=%s",
        context.article_correlation_id, "emergency_stock_selected" if selected else "emergency_stock_exhausted",
        selected.provider if selected else "", selected.asset_key if selected else "",
        bool(selected), context.original_rejection_reason, summary,
    )
    return ImageSearchResult(
        candidate=selected, downloaded=selected_download,
        providers_attempted=strict_result.providers_attempted,
        provider_failures=dict(strict_result.provider_failures),
        rescue_context=context, emergency_summary=summary,
        article_correlation_id=context.article_correlation_id,
    )
