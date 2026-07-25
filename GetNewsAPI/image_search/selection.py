from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Callable, Iterable

from config import (
    IMAGE_GLOBAL_CANDIDATE_LIMIT,
    IMAGE_LICENSE_ALLOWLIST,
    IMAGE_MIN_HEIGHT,
    IMAGE_MIN_WIDTH,
)

from .downloader import ImageDownloadError, ImageDownloader, perceptual_hash_distance
from .license_policy import ImageLicensePolicy
from .models import DownloadedImage, ImageCandidate, ImageSearchResult
from .provider import ImageSearchProvider, ProviderUnavailable
from .queries import build_image_queries
from .registry import build_provider_registry
from .reuse import (
    PHASH_REUSE_DISTANCE,
    candidate_recent_local_usage,
    candidate_recent_wp_usage,
    downloaded_recent_local_usage,
    load_image_usage,
    load_recent_wp_image_usage,
)
from .scoring import rank_candidates, score_candidate


logger = logging.getLogger(__name__)


def _reject(candidate: ImageCandidate, reason: str, counts: Counter[str]) -> None:
    counts[reason] += 1
    logger.info(
        "[IMG-V2] rejected provider=%s asset_key=%s reason=%s",
        candidate.provider,
        candidate.asset_key,
        reason,
    )


def _collect_provider_candidates(
    provider: ImageSearchProvider,
    queries: list[str],
) -> tuple[list[ImageCandidate], str | None]:
    candidates: list[ImageCandidate] = []
    for query in queries[:3]:
        try:
            query_candidates = provider.search(query)
        except Exception as exc:
            return candidates, _safe_provider_failure(exc)
        logger.info(
            "[IMG-V2] provider=%s query=%r candidates=%s",
            provider.provider_name,
            query,
            len(query_candidates),
        )
        candidates.extend(query_candidates)
    return candidates, None


def _safe_provider_failure(exc: Exception) -> str:
    if isinstance(exc, ProviderUnavailable):
        return str(exc)
    error_type = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__) or "Error"
    return f"error_type={error_type}"


def _candidate_identities(candidate: ImageCandidate) -> set[str]:
    values = {
        candidate.asset_key,
        candidate.canonical_source,
        candidate.source_page_url,
        candidate.url_hash,
    }
    return {value for value in values if value}


def _known_dimensions_are_valid(candidate: ImageCandidate) -> tuple[bool, str]:
    if candidate.width is not None and candidate.width < IMAGE_MIN_WIDTH:
        return False, "dimensions"
    if candidate.height is not None and candidate.height < IMAGE_MIN_HEIGHT:
        return False, "dimensions"
    if candidate.width and candidate.height and candidate.width < candidate.height:
        return False, "orientation"
    return True, ""


def _download_matches_seen(downloaded: DownloadedImage, seen: list[DownloadedImage]) -> bool:
    for prior in seen:
        if prior.content_sha256 == downloaded.content_sha256:
            return True
        distance = perceptual_hash_distance(prior.perceptual_hash, downloaded.perceptual_hash)
        if distance is not None and distance <= PHASH_REUSE_DISTANCE:
            return True
    return False


def _deduplicate_ranked_candidates(
    candidates: list[ImageCandidate],
    rejection_counts: Counter[str],
    candidate_limit: int,
) -> list[ImageCandidate]:
    parents = list(range(len(candidates)))

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
    for index, candidate in enumerate(candidates):
        for identity in sorted(_candidate_identities(candidate)):
            owner = identity_owner.setdefault(identity, index)
            union(index, owner)

    component_winner: dict[int, int] = {}
    for index in range(len(candidates)):
        root = find(index)
        component_winner[root] = min(component_winner.get(root, index), index)

    retained: list[ImageCandidate] = []
    for index, candidate in enumerate(candidates):
        if component_winner[find(index)] != index:
            _reject(candidate, "duplicate", rejection_counts)
            continue
        if len(retained) < candidate_limit:
            retained.append(candidate)
    return retained


def search_images(
    article: dict,
    *,
    providers: Iterable[ImageSearchProvider] | None = None,
    downloader: ImageDownloader | None = None,
    local_usage: list[dict] | None = None,
    recent_wp_usage: list[dict] | None = None,
    license_policy: ImageLicensePolicy | None = None,
    post_download_validator: Callable[[ImageCandidate, DownloadedImage], bool] | None = None,
    global_candidate_limit: int = IMAGE_GLOBAL_CANDIDATE_LIMIT,
) -> ImageSearchResult:
    provider_list = list(providers) if providers is not None else build_provider_registry()
    provider_names = tuple(provider.provider_name for provider in provider_list)
    if not provider_list:
        return ImageSearchResult(
            provider_failures={"registry": "no image search providers are enabled"},
            all_available_providers_exhausted=False,
        )

    queries = build_image_queries(article)
    logger.info("[IMG-V2] image queries=%s providers=%s", queries, provider_names)
    failures: dict[str, str] = {}
    collected: list[ImageCandidate] = []
    with ThreadPoolExecutor(max_workers=min(3, len(provider_list))) as executor:
        futures = {
            executor.submit(_collect_provider_candidates, provider, queries): provider
            for provider in provider_list
        }
        for future in as_completed(futures):
            provider = futures[future]
            try:
                provider_candidates, error = future.result()
            except Exception as exc:
                provider_candidates = []
                error = _safe_provider_failure(exc)
            collected.extend(provider_candidates)
            if error:
                failures[provider.provider_name] = error
                logger.warning(
                    "[IMG-V2] provider failed provider=%s error=%s",
                    provider.provider_name,
                    error,
                )

    policy = license_policy or ImageLicensePolicy(IMAGE_LICENSE_ALLOWLIST)
    local_entries = load_image_usage() if local_usage is None else local_usage
    wp_entries = load_recent_wp_image_usage() if recent_wp_usage is None else recent_wp_usage
    rejection_counts: Counter[str] = Counter()
    accepted: list[ImageCandidate] = []

    for candidate in collected:
        score_candidate(candidate, article)
        if candidate.final_score < candidate.provider_threshold:
            _reject(candidate, "threshold", rejection_counts)
            continue
        license_valid, reason = policy.validate(candidate)
        if not license_valid:
            _reject(candidate, reason or "license", rejection_counts)
            continue
        if (
            not candidate.usable_url
            or not (candidate.source_page_url or candidate.canonical_source)
            or not candidate.asset_key
            or not candidate.attribution_text
        ):
            _reject(candidate, "required_fields", rejection_counts)
            continue
        dimensions_valid, reason = _known_dimensions_are_valid(candidate)
        if not dimensions_valid:
            _reject(candidate, reason, rejection_counts)
            continue
        if candidate_recent_local_usage(candidate, local_entries):
            _reject(candidate, "recent_use", rejection_counts)
            continue
        if candidate_recent_wp_usage(candidate, wp_entries):
            _reject(candidate, "recent_use", rejection_counts)
            continue
        accepted.append(candidate)

    ranked = _deduplicate_ranked_candidates(
        rank_candidates(accepted),
        rejection_counts,
        max(0, global_candidate_limit),
    )
    image_downloader = downloader or ImageDownloader()
    seen_downloads: list[DownloadedImage] = []
    for candidate in ranked:
        try:
            downloaded = image_downloader.download(candidate)
        except ImageDownloadError as exc:
            _reject(candidate, exc.reason, rejection_counts)
            if exc.provider_unavailable:
                failures.setdefault(candidate.provider, f"download {exc.reason}")
                logger.warning(
                    "[IMG-V2] provider failed provider=%s error=download %s",
                    candidate.provider,
                    exc.reason,
                )
            continue
        if downloaded_recent_local_usage(downloaded, local_entries):
            _reject(candidate, "recent_use", rejection_counts)
            seen_downloads.append(downloaded)
            continue
        if _download_matches_seen(downloaded, seen_downloads):
            _reject(candidate, "duplicate", rejection_counts)
            continue
        if post_download_validator is not None:
            try:
                download_accepted = post_download_validator(candidate, downloaded)
            except Exception:
                download_accepted = False
            if not download_accepted:
                _reject(candidate, "conversion", rejection_counts)
                seen_downloads.append(downloaded)
                continue
        logger.info(
            "[IMG-V2] selected provider=%s asset_key=%s final_score=%.4f license=%s",
            candidate.provider,
            candidate.asset_key,
            candidate.final_score,
            candidate.license_name,
        )
        return ImageSearchResult(
            candidate=candidate,
            downloaded=downloaded,
            providers_attempted=provider_names,
            provider_failures=failures,
            rejection_counts=dict(rejection_counts),
            all_available_providers_exhausted=False,
        )

    exhausted = not failures
    if exhausted:
        logger.info(
            "[IMG-V2] all configured image search providers exhausted rejections=%s",
            dict(rejection_counts),
        )
    return ImageSearchResult(
        providers_attempted=provider_names,
        provider_failures=failures,
        rejection_counts=dict(rejection_counts),
        all_available_providers_exhausted=exhausted,
    )
