from __future__ import annotations

import hashlib
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from config import (
    IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED,
    IMAGE_GENERATION_ON_PROVIDER_ERROR,
    IMAGE_GLOBAL_CANDIDATE_LIMIT,
    IMAGE_LICENSE_ALLOWLIST,
    IMAGE_MIN_HEIGHT,
    IMAGE_MIN_WIDTH,
)

from .downloader import ImageDownloadError, ImageDownloader, perceptual_hash_distance
from .license_policy import ImageLicensePolicy
from .models import DownloadedImage, ImageCandidate, ImageSearchResult, RescueValidationResult
from .emergency import (
    create_rescue_context, emergency_summary, hard_fail_group, hard_fail_transport,
    mark_soft_rejection, remember_download, validation_outcome,
)
from .provider import ImageSearchProvider, ProviderUnavailable
from .queries import build_image_queries
from .registry import build_provider_registry
from .reuse import (
    PHASH_REUSE_DISTANCE,
    candidate_identity_values,
    candidate_recent_local_usage,
    candidate_recent_wp_usage,
    downloaded_recent_local_usage,
    load_image_usage,
    load_recent_wp_image_usage,
)
from .scoring import rank_candidates, score_candidate
from .context import context_rejection_reason


logger = logging.getLogger(__name__)


def _reject(
    candidate: ImageCandidate,
    reason: str,
    counts: Counter[str],
    provider_stats: dict[str, dict],
    correlation: str,
    *,
    detail: str = "",
) -> None:
    counts[reason] += 1
    rejected = provider_stats[candidate.provider]["rejected_by_reason"]
    rejected[reason] = rejected.get(reason, 0) + 1
    logger.info(
        "[IMG-V2] rejected article=%s provider=%s asset_key=%s reason=%s score=%.4f threshold=%.4f detail=%s",
        correlation,
        candidate.provider,
        candidate.asset_key,
        reason,
        candidate.final_score,
        candidate.provider_threshold,
        detail,
    )


def _collect_provider_candidates(
    provider: ImageSearchProvider,
    queries: list[str],
) -> tuple[list[ImageCandidate], str | None, dict]:
    candidates: list[ImageCandidate] = []
    stats = {"raw": 0, "normalized": 0, "rejected_by_reason": {}}
    for query in queries[:3]:
        try:
            provider.last_search_stats = {}
            provider.last_search_rejections = {}
            query_candidates = provider.search(query)
            if not isinstance(query_candidates, list) or not all(
                isinstance(candidate, ImageCandidate) for candidate in query_candidates
            ):
                raise ProviderUnavailable(provider.provider_name, "invalid_response")
        except Exception as exc:
            return candidates, _safe_provider_failure(exc), stats
        stats["raw"] += int(provider.last_search_stats.get("raw", len(query_candidates)))
        stats["normalized"] += len(query_candidates)
        for reason, count in provider.last_search_rejections.items():
            rejected = stats["rejected_by_reason"]
            rejected[reason] = rejected.get(reason, 0) + int(count)
        logger.info(
            "[IMG-V2] provider=%s query=%r candidates=%s",
            provider.provider_name,
            query,
            len(query_candidates),
        )
        candidates.extend(query_candidates)
    return candidates, None, stats


def _safe_provider_failure(exc: Exception) -> str:
    if isinstance(exc, ProviderUnavailable):
        return str(exc)
    error_type = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__) or "Error"
    return f"error_type={error_type}"


def _candidate_identities(candidate: ImageCandidate) -> set[str]:
    return candidate_identity_values(candidate)


def _known_dimensions_are_valid(candidate: ImageCandidate) -> tuple[bool, str]:
    if candidate.width is not None and candidate.width < IMAGE_MIN_WIDTH:
        return False, "dimensions_rejected"
    if candidate.height is not None and candidate.height < IMAGE_MIN_HEIGHT:
        return False, "dimensions_rejected"
    if candidate.width and candidate.height and candidate.width < candidate.height:
        return False, "orientation_rejected"
    return True, ""


def _download_matches_seen(downloaded: DownloadedImage, seen: list[DownloadedImage]) -> bool:
    for prior in seen:
        if prior.content_sha256 == downloaded.content_sha256:
            return True
        distance = perceptual_hash_distance(prior.perceptual_hash, downloaded.perceptual_hash)
        if distance is not None and distance <= PHASH_REUSE_DISTANCE:
            return True
    return False


@dataclass
class _IdentityGroup:
    variants: list[ImageCandidate]
    members: list[ImageCandidate]


def _group_ranked_candidates(
    candidates: list[ImageCandidate],
    linkage_candidates: list[ImageCandidate],
) -> list[_IdentityGroup]:
    """Keep delivery variants and transitive aliases, ordered by eligible rank."""
    parents = list(range(len(linkage_candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        parents[max(left_root, right_root)] = min(left_root, right_root)

    identity_owner: dict[str, int] = {}
    for index, candidate in enumerate(linkage_candidates):
        for identity in sorted(_candidate_identities(candidate)):
            owner = identity_owner.setdefault(identity, index)
            union(index, owner)

    members: dict[int, list[ImageCandidate]] = {}
    indices: dict[int, int] = {}
    for index, candidate in enumerate(linkage_candidates):
        members.setdefault(find(index), []).append(candidate)
        indices[id(candidate)] = index

    groups: dict[int, _IdentityGroup] = {}
    for candidate in candidates:
        root = find(indices[id(candidate)])
        # Ineligible linkage members carry aliases/history, never precedence.
        group = groups.setdefault(root, _IdentityGroup([], members[root]))
        group.variants.append(candidate)
    return list(groups.values())


def search_images(
    article: dict,
    *,
    providers: Iterable[ImageSearchProvider] | None = None,
    downloader: ImageDownloader | None = None,
    local_usage: list[dict] | None = None,
    recent_wp_usage: list[dict] | None = None,
    license_policy: ImageLicensePolicy | None = None,
    post_download_validator: Callable[[ImageCandidate, DownloadedImage], bool | RescueValidationResult] | None = None,
    global_candidate_limit: int = IMAGE_GLOBAL_CANDIDATE_LIMIT,
) -> ImageSearchResult:
    provider_list = list(providers) if providers is not None else build_provider_registry()
    provider_names = tuple(provider.provider_name for provider in provider_list)
    stable_field = article.get("id") or article.get("news_url") or article.get("url") or article.get("title") or "unknown"
    correlation = hashlib.sha256(str(stable_field).encode("utf-8")).hexdigest()[:12]
    queries = build_image_queries(article)
    logger.info("[IMG-V2] image article=%s queries=%s providers=%s", correlation, queries, provider_names)
    failures: dict[str, str] = {}
    collected: dict[str, list[ImageCandidate]] = {}
    provider_stats = {
        name: {
            "raw": 0, "normalized": 0, "above_threshold": 0, "eligible": 0,
            "download_attempted": 0, "selected": 0, "rejected_by_reason": {},
        }
        for name in provider_names
    }
    if not provider_list:
        failures["registry"] = "no image search providers are enabled"
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(provider_list)))) as executor:
        futures = {
            executor.submit(_collect_provider_candidates, provider, queries): provider
            for provider in provider_list
        }
        for future in as_completed(futures):
            provider = futures[future]
            try:
                provider_candidates, error, search_stats = future.result()
            except Exception as exc:
                provider_candidates = []
                error = _safe_provider_failure(exc)
                search_stats = {}
            collected[provider.provider_name] = provider_candidates
            provider_stats[provider.provider_name].update(search_stats)
            if error:
                failures[provider.provider_name] = error
                provider_stats[provider.provider_name]["rejected_by_reason"]["provider_failed"] = 1
                logger.warning(
                    "[IMG-V2] provider failed article=%s provider=%s reason=provider_failed error=%s",
                    correlation,
                    provider.provider_name,
                    error,
                )
            elif provider_stats[provider.provider_name]["raw"] == 0:
                logger.info(
                    "[IMG-V2] article=%s provider=%s reason=provider_no_results",
                    correlation, provider.provider_name,
                )

    policy = license_policy or ImageLicensePolicy(IMAGE_LICENSE_ALLOWLIST)
    local_entries = load_image_usage() if local_usage is None and provider_list else (local_usage or [])
    wp_entries = load_recent_wp_image_usage() if recent_wp_usage is None and provider_list else (recent_wp_usage or [])
    rejection_counts: Counter[str] = Counter()
    ordered: list[ImageCandidate] = []
    for provider_name in provider_names:
        accepted: list[ImageCandidate] = []
        for candidate in collected.get(provider_name, []):
            score_candidate(candidate, article)
            context_reason = context_rejection_reason(candidate, article)
            if context_reason:
                _reject(candidate, context_reason, rejection_counts, provider_stats, correlation)
                continue
            reason = ""
            if candidate.final_score < candidate.provider_threshold:
                reason = "score_below_threshold"
            else:
                provider_stats[provider_name]["above_threshold"] += 1
                license_valid, _license_reason = policy.validate(candidate)
                if not license_valid:
                    reason = "license_rejected"
                elif (
                    not candidate.usable_url
                    or not (candidate.source_page_url or candidate.canonical_source)
                    or not candidate.asset_key
                    or not candidate.attribution_text
                ):
                    reason = "required_fields"
                else:
                    dimensions_valid, dimensions_reason = _known_dimensions_are_valid(candidate)
                    if not dimensions_valid:
                        reason = dimensions_reason
            if reason:
                _reject(candidate, reason, rejection_counts, provider_stats, correlation)
                continue
            accepted.append(candidate)
        # Provider precedence is stronger than any cross-provider score difference.
        ordered.extend(rank_candidates(accepted))

    groups = _group_ranked_candidates(
        ordered, [candidate for name in provider_names for candidate in collected.get(name, [])],
    )
    image_downloader = downloader or ImageDownloader()
    rescue_context = create_rescue_context(
        [candidate for name in provider_names for candidate in collected.get(name, [])],
        provider_precedence=provider_names, downloader=image_downloader, license_policy=policy,
        local_usage=local_entries, recent_wp_usage=wp_entries, article_correlation_id=correlation,
        article_context=article,
    )
    attempt_plan: list[tuple[int, ImageCandidate]] = []
    for group_index, group in enumerate(groups):
        reason = ""
        if any(candidate_recent_local_usage(member, local_entries) for member in group.members):
            reason = "local_history_identity_reuse"
        elif any(candidate_recent_wp_usage(member, wp_entries) for member in group.members):
            reason = "wordpress_history_reuse"
        seen_transports: set[tuple[str, str]] = set()
        for candidate in group.variants:
            if reason:
                _reject(candidate, reason, rejection_counts, provider_stats, correlation)
                continue
            provider_stats[candidate.provider]["eligible"] += 1
            transport = (candidate.provider, candidate.usable_url)
            if transport in seen_transports:
                _reject(candidate, "duplicate_identity", rejection_counts, provider_stats, correlation)
                continue
            seen_transports.add(transport)
            attempt_plan.append((group_index, candidate))

    seen_downloads: list[tuple[int, DownloadedImage]] = []
    blocked_groups: dict[int, str] = {}
    selected: ImageCandidate | None = None
    selected_download: DownloadedImage | None = None
    budget_exhausted = False
    attempts = 0
    for group_index, candidate in attempt_plan:
        if group_index in blocked_groups:
            _reject(candidate, blocked_groups[group_index], rejection_counts, provider_stats, correlation)
            mark_soft_rejection(rescue_context, candidate, blocked_groups[group_index], group=True)
            if blocked_groups[group_index] == "duplicate_content":
                hard_fail_group(rescue_context, candidate, "duplicate_content")
            continue
        if attempts >= max(0, global_candidate_limit):
            budget_exhausted = True
            break
        attempts += 1
        provider_stats[candidate.provider]["download_attempted"] += 1
        rescue_context.strict_attempted_transports.add((candidate.provider, candidate.usable_url))
        try:
            downloaded = image_downloader.download(candidate)
        except ImageDownloadError as exc:
            hard_fail_transport(rescue_context, candidate, "download_failed")
            reason = {
                "dimensions": "dimensions_rejected", "orientation": "orientation_rejected",
            }.get(exc.reason, "download_failed")
            _reject(candidate, reason, rejection_counts, provider_stats, correlation, detail=exc.reason)
            if exc.provider_unavailable:
                failures.setdefault(candidate.provider, f"download {exc.reason}")
                reasons = provider_stats[candidate.provider]["rejected_by_reason"]
                reasons["provider_failed"] = reasons.get("provider_failed", 0) + 1
                logger.warning(
                    "[IMG-V2] provider failed article=%s provider=%s reason=provider_failed error=download %s",
                    correlation,
                    candidate.provider,
                    exc.reason,
                )
            continue
        remember_download(rescue_context, candidate, downloaded)
        prior_usage = downloaded_recent_local_usage(downloaded, local_entries)
        if prior_usage:
            reason = (
                "local_history_sha_reuse"
                if prior_usage.get("content_sha256") == downloaded.content_sha256
                else "local_history_dhash_reuse"
            )
            _reject(candidate, reason, rejection_counts, provider_stats, correlation)
            mark_soft_rejection(rescue_context, candidate, reason, group=True)
            blocked_groups[group_index] = reason
            seen_downloads.append((group_index, downloaded))
            continue
        if _download_matches_seen(downloaded, [prior for owner, prior in seen_downloads if owner != group_index]):
            _reject(candidate, "duplicate_content", rejection_counts, provider_stats, correlation)
            hard_fail_group(rescue_context, candidate, "duplicate_content")
            blocked_groups[group_index] = "duplicate_content"
            continue
        if post_download_validator is not None:
            try:
                download_accepted, validation_reason = validation_outcome(post_download_validator(candidate, downloaded))
            except Exception:
                download_accepted, validation_reason = False, "conversion_failed"
            if not download_accepted:
                _reject(candidate, validation_reason, rejection_counts, provider_stats, correlation)
                hard_fail_transport(rescue_context, candidate, validation_reason)
                # A conversion failure may be specific to this delivery. Keep
                # cross-group content deduplication without poisoning its own
                # alternate URL, even if that alternate delivers identical bytes.
                if validation_reason == "conversion_failed":
                    seen_downloads.append((group_index, downloaded))
                continue
        logger.info(
            "[IMG-V2] selected provider=%s asset_key=%s final_score=%.4f license=%s",
            candidate.provider,
            candidate.asset_key,
            candidate.final_score,
            candidate.license_name,
        )
        selected, selected_download = candidate, downloaded
        provider_stats[candidate.provider]["selected"] = 1
        break

    exhausted = bool(provider_list) and selected is None and not failures and not budget_exhausted
    generation_allowed = selected is None and (
        not IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED
        or exhausted
        or IMAGE_GENERATION_ON_PROVIDER_ERROR
    )
    for name, stats in provider_stats.items():
        reuse_count = sum(
            count for reason, count in stats["rejected_by_reason"].items()
            if reason.startswith("local_history_") or reason == "wordpress_history_reuse"
        )
        if stats["above_threshold"] and reuse_count == stats["above_threshold"]:
            logger.info(
                "[IMG-V2] article=%s provider=%s reason=all_high_scoring_candidates_recently_used",
                correlation, name,
            )
        logger.info("[IMG-V2] provider_summary article=%s provider=%s counts=%s", correlation, name, stats)
    outcome = (
        "selected" if selected else "search_budget_exhausted" if budget_exhausted
        else "provider_failed" if failures else "all_providers_exhausted"
    )
    logger.info(
        "[IMG-V2] terminal article=%s reason=%s providers_attempted=%s provider_failures=%s "
        "selected_provider=%s selected_asset_key=%s all_available_providers_exhausted=%s "
        "search_budget_exhausted=%s generation_allowed=%s",
        correlation, outcome, provider_names, failures,
        selected.provider if selected else "", selected.asset_key if selected else "",
        exhausted, budget_exhausted, generation_allowed,
    )
    return ImageSearchResult(
        candidate=selected,
        downloaded=selected_download,
        providers_attempted=provider_names,
        provider_failures=failures,
        rejection_counts=dict(rejection_counts),
        all_available_providers_exhausted=exhausted,
        search_budget_exhausted=budget_exhausted,
        provider_stats=provider_stats,
        article_correlation_id=correlation,
        generation_allowed=generation_allowed,
        rescue_context=rescue_context,
        emergency_summary=emergency_summary(rescue_context),
    )
