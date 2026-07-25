from __future__ import annotations

import re
from typing import Any


STOPWORDS = {
    "a", "about", "after", "against", "and", "are", "as", "at", "be", "by", "for",
    "from", "has", "have", "in", "into", "is", "it", "its", "of", "on", "or", "over",
    "says", "the", "to", "with",
}
REGULATION_TERMS = {"regulation", "regulatory", "law", "legal", "sec", "government", "policy"}
MARKET_TERMS = {"etf", "liquidity", "market", "markets", "price", "trading", "outflow", "inflow"}


def normalize_terms(text: str | None, *, max_terms: int | None = None) -> list[str]:
    normalized = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    terms: list[str] = []
    for token in normalized.split():
        if token in STOPWORDS or (len(token) <= 1 and token != "x"):
            continue
        if token not in terms:
            terms.append(token)
        if max_terms and len(terms) >= max_terms:
            break
    return terms


def article_blob(article: dict[str, Any]) -> str:
    return " ".join(
        str(article.get(key) or "")
        for key in ("seo_focus", "title", "hashtags", "category", "tickers", "source_name")
    ).lower()


def _generic_query(article_terms: set[str]) -> str:
    if article_terms & {"bitcoin", "btc"}:
        return "bitcoin cryptocurrency"
    if article_terms & {"ethereum", "eth"}:
        return "ethereum blockchain"
    if article_terms & {"solana", "sol"}:
        return "solana blockchain"
    if article_terms & REGULATION_TERMS:
        return "crypto regulation"
    if article_terms & MARKET_TERMS:
        return "crypto market"
    return "blockchain finance"


def build_image_queries(article: dict[str, Any]) -> list[str]:
    """Match the V1 query construction while keeping it provider-neutral."""
    queries: list[str] = []
    seo_terms = normalize_terms(str(article.get("seo_focus") or ""), max_terms=5)
    if seo_terms:
        queries.append(" ".join(seo_terms))

    topic_text = " ".join(
        str(article.get(key) or "") for key in ("title", "hashtags", "category", "tickers")
    )
    topic_terms = normalize_terms(topic_text, max_terms=5)
    if topic_terms:
        queries.append(" ".join(topic_terms[:5]))

    queries.append(_generic_query(set(normalize_terms(article_blob(article)))))
    unique: list[str] = []
    for query in queries:
        compact = " ".join(normalize_terms(query, max_terms=5))
        if compact and compact not in unique:
            unique.append(compact)
        if len(unique) >= 3:
            break
    return unique or ["blockchain finance"]
