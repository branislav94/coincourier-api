from __future__ import annotations

from typing import Any

from .models import ImageCandidate
from .queries import article_blob, normalize_terms


CRYPTO_TERMS = {
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "blockchain", "cryptocurrency",
    "crypto", "trading", "market", "markets", "finance", "financial", "etf", "regulation",
    "staking", "defi", "token", "tokens", "exchange", "liquidity",
}
GENERIC_ONLY_TERMS = {"abstract", "business", "laptop", "money", "office", "technology"}
REGULATION_TERMS = {"regulation", "regulatory", "law", "legal", "sec", "government", "policy"}
MARKET_TERMS = {"etf", "liquidity", "market", "markets", "price", "trading", "outflow", "inflow"}
CHAIN_TERMS = {"bitcoin", "btc", "ethereum", "eth", "solana", "sol"}


def quality_score(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.35
    if width < 640 or height < 360:
        return 0.0
    ratio = width / max(height, 1)
    score = 0.55
    if width >= 1200 and height >= 675:
        score += 0.30
    if 1.3 <= ratio <= 2.0:
        score += 0.15
    elif ratio < 1.0:
        score -= 0.35
    return max(0.0, min(1.0, score))


def relevance_score(query_terms: set[str], metadata_terms: set[str]) -> float:
    if not query_terms:
        return 0.0
    coverage = len(query_terms & metadata_terms) / len(query_terms)
    crypto_query_terms = query_terms & CRYPTO_TERMS
    crypto_coverage = (
        len(crypto_query_terms & metadata_terms) / len(crypto_query_terms)
        if crypto_query_terms else 0.0
    )
    exact_crypto_bonus = 0.15 if crypto_query_terms & metadata_terms else 0.0
    return max(0.0, min(1.0, coverage * 0.80 + crypto_coverage * 0.20 + exact_crypto_bonus))


def topic_score(article_terms: set[str], metadata_terms: set[str]) -> float:
    score = 0.0
    if article_terms & CHAIN_TERMS and metadata_terms & (CHAIN_TERMS | {"crypto", "cryptocurrency", "blockchain", "trading"}):
        score += 0.70
    if article_terms & REGULATION_TERMS and metadata_terms & (REGULATION_TERMS | {"finance", "financial", "market"}):
        score += 0.70
    if article_terms & MARKET_TERMS and metadata_terms & (MARKET_TERMS | {"finance", "financial", "trading"}):
        score += 0.70
    if article_terms & {"defi", "staking", "token", "tokens"} and metadata_terms & {"defi", "staking", "token", "tokens", "blockchain", "finance"}:
        score += 0.70
    if metadata_terms & CRYPTO_TERMS:
        score += 0.25
    if metadata_terms & GENERIC_ONLY_TERMS and not metadata_terms & CRYPTO_TERMS:
        score -= 0.25
    return max(0.0, min(1.0, score))


def popularity_score(provider: str, metadata: dict[str, Any]) -> float:
    if provider == "pexels":
        return 0.65 if metadata.get("liked") else 0.45

    def safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    views = safe_int(metadata.get("views"))
    downloads = safe_int(metadata.get("downloads"))
    likes = safe_int(metadata.get("likes"))
    raw = min(1.0, (views / 50000) * 0.45 + (downloads / 10000) * 0.35 + (likes / 1000) * 0.20)
    return max(0.25, raw)


def score_candidate(candidate: ImageCandidate, article: dict[str, Any]) -> ImageCandidate:
    metadata_text = str(candidate.metadata.get("metadata_text") or "")
    query_terms = set(normalize_terms(candidate.query))
    metadata_terms = set(normalize_terms(metadata_text))
    article_terms = set(normalize_terms(article_blob(article)))
    candidate.relevance_score = round(relevance_score(query_terms, metadata_terms), 4)
    candidate.topic_score = round(topic_score(article_terms, metadata_terms), 4)
    candidate.quality_score = round(quality_score(candidate.width, candidate.height), 4)
    candidate.popularity_score = round(popularity_score(candidate.provider, candidate.metadata), 4)
    candidate.final_score = round(
        candidate.relevance_score * 0.55
        + candidate.topic_score * 0.20
        + candidate.quality_score * 0.15
        + candidate.popularity_score * 0.10,
        4,
    )
    return candidate


def normalized_global_score(candidate: ImageCandidate) -> float:
    """Map the provider threshold to 0 and a perfect score to 1, then clamp."""
    threshold = max(0.0, min(0.9999, candidate.provider_threshold))
    return round(max(0.0, min(1.0, (candidate.final_score - threshold) / (1.0 - threshold))), 6)


def rank_candidates(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    for candidate in candidates:
        candidate.metadata["normalized_global_score"] = normalized_global_score(candidate)
    return sorted(
        candidates,
        key=lambda candidate: (
            -float(candidate.metadata["normalized_global_score"]),
            -candidate.final_score,
            candidate.provider_rank,
            candidate.provider,
            candidate.asset_key,
            candidate.canonical_source,
            candidate.source_page_url,
            candidate.url_hash,
            candidate.query,
        ),
    )
