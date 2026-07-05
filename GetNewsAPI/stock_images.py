from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mysql.connector
import requests

from config import (
    PEXELS_API_KEY,
    PEXELS_ENABLED,
    PEXELS_MIN_SCORE,
    PEXELS_ORIENTATION,
    PEXELS_PER_PAGE,
    PIXABAY_API_KEY,
    PIXABAY_ENABLED,
    PIXABAY_MIN_SCORE,
    PIXABAY_ORIENTATION,
    PIXABAY_PER_PAGE,
    STOCK_IMAGE_CACHE_HOURS,
    STOCK_IMAGE_REUSE_CHECK_WP_HISTORY,
    STOCK_IMAGE_REUSE_WINDOW_DAYS,
    STOCK_IMAGE_TIMEOUT_SECONDS,
    STOCK_IMAGE_USAGE_PATH,
    WP_DB_CONFIG,
)


logger = logging.getLogger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
PIXABAY_SEARCH_URL = "https://pixabay.com/api/"

STOPWORDS = {
    "a",
    "about",
    "after",
    "against",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "over",
    "says",
    "the",
    "to",
    "with",
}

CRYPTO_TERMS = {
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "solana",
    "sol",
    "blockchain",
    "cryptocurrency",
    "crypto",
    "trading",
    "market",
    "markets",
    "finance",
    "financial",
    "etf",
    "regulation",
    "staking",
    "defi",
    "token",
    "tokens",
    "exchange",
    "liquidity",
}

GENERIC_ONLY_TERMS = {
    "abstract",
    "business",
    "laptop",
    "money",
    "office",
    "technology",
}

REGULATION_TERMS = {"regulation", "regulatory", "law", "legal", "sec", "government", "policy"}
MARKET_TERMS = {"etf", "liquidity", "market", "markets", "price", "trading", "outflow", "inflow"}
CHAIN_TERMS = {"bitcoin", "btc", "ethereum", "eth", "solana", "sol"}


@dataclass
class StockImageCandidate:
    provider: str
    image_url: str
    query: str
    score: float
    threshold: float
    width: int | None = None
    height: int | None = None
    credit_name: str = ""
    credit_url: str = ""
    provider_asset_id: str = ""
    asset_key: str = ""
    metadata_text: str = ""
    score_parts: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.asset_key:
            return
        if self.provider_asset_id:
            self.asset_key = f"{self.provider}:{self.provider_asset_id}"
            return
        image_hash = hashlib.sha256((self.image_url or self.credit_url or "").encode("utf-8")).hexdigest()
        self.asset_key = f"{self.provider}:url:{image_hash}"

    @property
    def credit_text(self) -> str:
        if self.provider == "pexels" and self.credit_name and self.credit_url:
            return f"Photo by {self.credit_name} on Pexels: {self.credit_url}"
        if self.provider == "pixabay" and self.credit_name and self.credit_url:
            return f"Image by {self.credit_name} on Pixabay: {self.credit_url}"
        return ""

    @property
    def rejection_reason(self) -> str:
        if self.score < self.threshold:
            return f"score {self.score:.2f} below threshold {self.threshold:.2f}"
        return ""


def normalize_terms(text: str | None, *, max_terms: int | None = None) -> list[str]:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    terms: list[str] = []
    for token in text.split():
        if token in STOPWORDS:
            continue
        if len(token) <= 1 and token not in {"x"}:
            continue
        if token not in terms:
            terms.append(token)
        if max_terms and len(terms) >= max_terms:
            break
    return terms


def _article_blob(article: dict[str, Any]) -> str:
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
    if article_terms & {"defi", "staking"}:
        return "blockchain finance"
    return "blockchain finance"


def build_stock_queries(article: dict[str, Any]) -> list[str]:
    queries: list[str] = []

    seo_terms = normalize_terms(str(article.get("seo_focus") or ""), max_terms=5)
    if seo_terms:
        queries.append(" ".join(seo_terms))

    topic_text = " ".join(
        str(article.get(key) or "")
        for key in ("title", "hashtags", "category", "tickers")
    )
    topic_terms = normalize_terms(topic_text, max_terms=5)
    if topic_terms:
        queries.append(" ".join(topic_terms[:5]))

    article_terms = set(normalize_terms(_article_blob(article)))
    queries.append(_generic_query(article_terms))

    unique: list[str] = []
    for query in queries:
        compact = " ".join(normalize_terms(query, max_terms=5))
        if compact and compact not in unique:
            unique.append(compact)
        if len(unique) >= 3:
            break
    return unique or ["blockchain finance"]


def _cache_root() -> Path:
    return Path(os.getenv("STOCK_IMAGE_CACHE_DIR", "/app/cache/stock_images"))


def _usage_path() -> Path:
    return Path(STOCK_IMAGE_USAGE_PATH)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_used_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _age_days(used_at: datetime) -> int:
    return max(0, (_utc_now() - used_at).days)


def _image_url_hash(image_url: str | None) -> str:
    return hashlib.sha256((image_url or "").encode("utf-8")).hexdigest()


def prune_stock_image_usage(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = _utc_now() - timedelta(days=STOCK_IMAGE_REUSE_WINDOW_DAYS)
    fresh: list[dict[str, Any]] = []
    for entry in entries:
        used_at = _parse_used_at(str(entry.get("used_at") or ""))
        if used_at and used_at >= cutoff:
            fresh.append(entry)
    return fresh


def load_stock_image_usage() -> list[dict[str, Any]]:
    path = _usage_path()
    try:
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return prune_stock_image_usage([entry for entry in payload if isinstance(entry, dict)])
    except Exception:
        logger.exception("[IMG] stock_image_usage read failed path=%s", path)
        return []


def _write_stock_image_usage(entries: list[dict[str, Any]]) -> None:
    path = _usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(entries, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        logger.exception("[IMG] stock_image_usage write failed path=%s", path)


def record_stock_image_usage(meta: dict[str, Any], post_id: int, title: str) -> None:
    provider = str(meta.get("provider") or "").lower()
    if provider not in {"pexels", "pixabay"}:
        return
    entry = {
        "provider": provider,
        "asset_key": meta.get("asset_key") or "",
        "provider_asset_id": meta.get("provider_asset_id") or "",
        "image_url_hash": meta.get("image_url_hash") or "",
        "credit_url": meta.get("credit_url") or "",
        "credit_name": meta.get("credit_name") or "",
        "query": meta.get("query") or "",
        "score": meta.get("score"),
        "post_id": post_id,
        "title": title,
        "used_at": _utc_now().isoformat().replace("+00:00", "Z"),
    }
    entries = prune_stock_image_usage(load_stock_image_usage())
    entries.append(entry)
    _write_stock_image_usage(entries)
    logger.info(
        "[IMG] recorded stock image usage provider=%s asset_key=%s post_id=%s",
        provider,
        entry["asset_key"],
        post_id,
    )


def _get_wp_prefix(conn) -> str:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name LIKE %s
            LIMIT 1
            """,
            ("%options",),
        )
        row = cur.fetchone()
        return row[0].replace("options", "") if row else "wp_"
    finally:
        cur.close()


def load_recent_wp_stock_usage() -> list[dict[str, Any]]:
    if not STOCK_IMAGE_REUSE_CHECK_WP_HISTORY:
        return []
    cutoff = (_utc_now() - timedelta(days=STOCK_IMAGE_REUSE_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with mysql.connector.connect(**WP_DB_CONFIG) as conn:
            prefix = _get_wp_prefix(conn)
            posts_table = f"`{prefix}posts`"
            cur = conn.cursor(dictionary=True)
            try:
                cur.execute(
                    f"""
                    SELECT ID, post_date_gmt, post_excerpt, post_content, guid
                    FROM {posts_table}
                    WHERE post_type = 'attachment'
                      AND post_date_gmt >= %s
                    """,
                    (cutoff,),
                )
                return list(cur.fetchall())
            finally:
                cur.close()
    except Exception:
        logger.warning("[IMG] WP-history stock image reuse check failed; using local stock_image_usage only", exc_info=True)
        return []


def _candidate_recent_local_usage(
    candidate: StockImageCandidate,
    local_usage: list[dict[str, Any]],
) -> tuple[dict[str, Any], int] | None:
    for entry in local_usage:
        used_at = _parse_used_at(str(entry.get("used_at") or ""))
        if not used_at:
            continue
        if entry.get("asset_key") and entry.get("asset_key") == candidate.asset_key:
            return entry, _age_days(used_at)
        if candidate.credit_url and entry.get("credit_url") == candidate.credit_url:
            return entry, _age_days(used_at)
        if entry.get("image_url_hash") and entry.get("image_url_hash") == _image_url_hash(candidate.image_url):
            return entry, _age_days(used_at)
    return None


def _candidate_recent_wp_usage(
    candidate: StockImageCandidate,
    recent_wp_usage: list[dict[str, Any]],
) -> tuple[dict[str, Any], int] | None:
    terms = [
        candidate.credit_url,
        candidate.provider_asset_id,
        candidate.image_url,
    ]
    terms = [str(term) for term in terms if term]
    for row in recent_wp_usage:
        blob = " ".join(
            str(row.get(key) or "")
            for key in ("post_excerpt", "post_content", "guid")
        )
        if not blob:
            continue
        if any(term in blob for term in terms):
            used_at = row.get("post_date_gmt")
            if isinstance(used_at, datetime):
                if used_at.tzinfo is None:
                    used_at = used_at.replace(tzinfo=timezone.utc)
                return row, _age_days(used_at.astimezone(timezone.utc))
            parsed = _parse_used_at(str(used_at or ""))
            return row, _age_days(parsed) if parsed else 0
    return None


def _cache_path(provider: str, query: str) -> Path:
    key = hashlib.sha256(f"{provider}:{query}".encode("utf-8")).hexdigest()
    return _cache_root() / provider / f"{key}.json"


def _read_cache(provider: str, query: str) -> dict[str, Any] | None:
    if STOCK_IMAGE_CACHE_HOURS <= 0:
        return None
    path = _cache_path(provider, query)
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(payload.get("cached_at", 0)) > STOCK_IMAGE_CACHE_HOURS * 3600:
            return None
        return payload.get("response")
    except Exception:
        logger.exception("[IMG] stock cache read failed provider=%s query=%r", provider, query)
        return None


def _write_cache(provider: str, query: str, response: dict[str, Any]) -> None:
    if STOCK_IMAGE_CACHE_HOURS <= 0:
        return
    path = _cache_path(provider, query)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cached_at": time.time(), "response": response}, ensure_ascii=True),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("[IMG] stock cache write failed provider=%s query=%r", provider, query)


def _get_json(provider: str, query: str, url: str, **kwargs: Any) -> dict[str, Any] | None:
    cached = _read_cache(provider, query)
    if cached is not None:
        return cached
    try:
        response = requests.get(url, timeout=STOCK_IMAGE_TIMEOUT_SECONDS, **kwargs)
        response.raise_for_status()
        data = response.json()
        _write_cache(provider, query, data)
        return data
    except Exception:
        logger.exception("[IMG] %s stock request failed query=%r", provider, query)
        return None


def _quality_score(width: int | None, height: int | None) -> float:
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


def _keyword_score(query_terms: set[str], metadata_terms: set[str]) -> float:
    if not query_terms:
        return 0.0
    coverage = len(query_terms & metadata_terms) / len(query_terms)
    crypto_query_terms = query_terms & CRYPTO_TERMS
    crypto_coverage = (
        len(crypto_query_terms & metadata_terms) / len(crypto_query_terms)
        if crypto_query_terms
        else 0.0
    )
    exact_crypto_bonus = 0.15 if crypto_query_terms & metadata_terms else 0.0
    return max(0.0, min(1.0, coverage * 0.80 + crypto_coverage * 0.20 + exact_crypto_bonus))


def _topic_score(article_terms: set[str], metadata_terms: set[str]) -> float:
    score = 0.0
    if article_terms & CHAIN_TERMS:
        if metadata_terms & (CHAIN_TERMS | {"crypto", "cryptocurrency", "blockchain", "trading"}):
            score += 0.70
    if article_terms & REGULATION_TERMS:
        if metadata_terms & (REGULATION_TERMS | {"finance", "financial", "market"}):
            score += 0.70
    if article_terms & MARKET_TERMS:
        if metadata_terms & (MARKET_TERMS | {"finance", "financial", "trading"}):
            score += 0.70
    if article_terms & {"defi", "staking", "token", "tokens"}:
        if metadata_terms & {"defi", "staking", "token", "tokens", "blockchain", "finance"}:
            score += 0.70
    if metadata_terms & CRYPTO_TERMS:
        score += 0.25
    if metadata_terms & GENERIC_ONLY_TERMS and not (metadata_terms & CRYPTO_TERMS):
        score -= 0.25
    return max(0.0, min(1.0, score))


def _popularity_score(provider: str, item: dict[str, Any]) -> float:
    if provider == "pexels":
        return 0.65 if item.get("liked") else 0.45
    views = int(item.get("views") or 0)
    downloads = int(item.get("downloads") or 0)
    likes = int(item.get("likes") or 0)
    raw = min(1.0, (views / 50000) * 0.45 + (downloads / 10000) * 0.35 + (likes / 1000) * 0.20)
    return max(0.25, raw)


def _score_candidate(
    provider: str,
    query: str,
    article: dict[str, Any],
    metadata_text: str,
    width: int | None,
    height: int | None,
    item: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    query_terms = set(normalize_terms(query))
    metadata_terms = set(normalize_terms(metadata_text))
    article_terms = set(normalize_terms(_article_blob(article)))

    keyword = _keyword_score(query_terms, metadata_terms)
    topic = _topic_score(article_terms, metadata_terms)
    quality = _quality_score(width, height)
    popularity = _popularity_score(provider, item)
    score = keyword * 0.55 + topic * 0.20 + quality * 0.15 + popularity * 0.10
    parts = {
        "keyword": round(keyword, 4),
        "topic": round(topic, 4),
        "quality": round(quality, 4),
        "popularity": round(popularity, 4),
    }
    return round(score, 4), parts


def _pexels_candidates(query: str, article: dict[str, Any]) -> list[StockImageCandidate]:
    if not PEXELS_ENABLED or not PEXELS_API_KEY:
        return []
    data = _get_json(
        "pexels",
        query,
        PEXELS_SEARCH_URL,
        headers={"Authorization": PEXELS_API_KEY},
        params={
            "query": query,
            "orientation": PEXELS_ORIENTATION,
            "per_page": PEXELS_PER_PAGE,
        },
    )
    if not data:
        return []

    candidates: list[StockImageCandidate] = []
    for photo in data.get("photos", []):
        src = photo.get("src") or {}
        image_url = src.get("large2x") or src.get("large") or src.get("original")
        if not image_url:
            continue
        metadata_text = " ".join(
            str(photo.get(key) or "") for key in ("alt", "url", "photographer")
        )
        score, parts = _score_candidate(
            "pexels",
            query,
            article,
            metadata_text,
            photo.get("width"),
            photo.get("height"),
            photo,
        )
        candidates.append(
            StockImageCandidate(
                provider="pexels",
                image_url=image_url,
                query=query,
                score=score,
                threshold=PEXELS_MIN_SCORE,
                width=photo.get("width"),
                height=photo.get("height"),
                credit_name=str(photo.get("photographer") or ""),
                credit_url=str(photo.get("url") or ""),
                provider_asset_id=str(photo.get("id") or ""),
                metadata_text=metadata_text,
                score_parts=parts,
            )
        )
    return candidates


def _pixabay_candidates(query: str, article: dict[str, Any]) -> list[StockImageCandidate]:
    if not PIXABAY_ENABLED or not PIXABAY_API_KEY:
        return []
    data = _get_json(
        "pixabay",
        query,
        PIXABAY_SEARCH_URL,
        params={
            "key": PIXABAY_API_KEY,
            "q": query,
            "image_type": "photo",
            "orientation": PIXABAY_ORIENTATION,
            "safesearch": "true",
            "per_page": PIXABAY_PER_PAGE,
        },
    )
    if not data:
        return []

    candidates: list[StockImageCandidate] = []
    for hit in data.get("hits", []):
        image_url = hit.get("largeImageURL") or hit.get("webformatURL")
        if not image_url:
            continue
        metadata_text = " ".join(
            str(hit.get(key) or "") for key in ("tags", "pageURL", "user")
        )
        score, parts = _score_candidate(
            "pixabay",
            query,
            article,
            metadata_text,
            hit.get("imageWidth"),
            hit.get("imageHeight"),
            hit,
        )
        candidates.append(
            StockImageCandidate(
                provider="pixabay",
                image_url=image_url,
                query=query,
                score=score,
                threshold=PIXABAY_MIN_SCORE,
                width=hit.get("imageWidth"),
                height=hit.get("imageHeight"),
                credit_name=str(hit.get("user") or ""),
                credit_url=str(hit.get("pageURL") or ""),
                provider_asset_id=str(hit.get("id") or ""),
                metadata_text=metadata_text,
                score_parts=parts,
            )
        )
    return candidates


def _best_for_provider(
    provider: str,
    article: dict[str, Any],
    queries: list[str],
    *,
    local_usage: list[dict[str, Any]],
    recent_wp_usage: list[dict[str, Any]],
) -> StockImageCandidate | None:
    search_fn = _pexels_candidates if provider == "pexels" else _pixabay_candidates
    best: StockImageCandidate | None = None

    for query in queries[:3]:
        candidates = sorted(search_fn(query, article), key=lambda c: c.score, reverse=True)
        if not candidates:
            logger.info("[IMG] %s returned no stock candidates query=%r", provider, query)
            continue
        for candidate in candidates:
            if not best or candidate.score > best.score:
                best = candidate
            if candidate.score < candidate.threshold:
                logger.info(
                    "[IMG] provider=%s rejected query=%r score=%.2f threshold=%.2f reason=%s dims=%sx%s parts=%s",
                    provider,
                    candidate.query,
                    candidate.score,
                    candidate.threshold,
                    candidate.rejection_reason,
                    candidate.width,
                    candidate.height,
                    candidate.score_parts,
                )
                break

            recent_local = _candidate_recent_local_usage(candidate, local_usage)
            if recent_local:
                _entry, age_days = recent_local
                logger.info(
                    "[IMG] provider=%s skipped recently used stock image asset_key=%s age_days=%s query=%r score=%.2f",
                    provider,
                    candidate.asset_key,
                    age_days,
                    candidate.query,
                    candidate.score,
                )
                continue

            recent_wp = _candidate_recent_wp_usage(candidate, recent_wp_usage)
            if recent_wp:
                _row, age_days = recent_wp
                logger.info(
                    "[IMG] provider=%s skipped WP-history stock image credit_url=%s age_days=%s query=%r score=%.2f",
                    provider,
                    candidate.credit_url,
                    age_days,
                    candidate.query,
                    candidate.score,
                )
                continue

            logger.info(
                "[IMG] provider=%s selected fresh stock image asset_key=%s query=%r score=%.2f",
                provider,
                candidate.asset_key,
                candidate.query,
                candidate.score,
            )
            logger.info(
                "[IMG] provider=%s accepted query=%r score=%.2f threshold=%.2f dims=%sx%s credit=%r parts=%s",
                provider,
                candidate.query,
                candidate.score,
                candidate.threshold,
                candidate.width,
                candidate.height,
                candidate.credit_name,
                candidate.score_parts,
            )
            return candidate

    if best:
        logger.info(
            "[IMG] provider=%s rejected best_query=%r score=%.2f threshold=%.2f reason=%s",
            provider,
            best.query,
            best.score,
            best.threshold,
            best.rejection_reason,
        )
    return None


def find_stock_image(
    article: dict[str, Any],
    providers: tuple[str, ...] = ("pexels", "pixabay"),
) -> StockImageCandidate | None:
    queries = build_stock_queries(article)
    logger.info("[IMG] stock queries=%s", queries)
    local_usage = load_stock_image_usage()
    recent_wp_usage = load_recent_wp_stock_usage()
    for provider in providers:
        candidate = _best_for_provider(
            provider,
            article,
            queries,
            local_usage=local_usage,
            recent_wp_usage=recent_wp_usage,
        )
        if candidate:
            return candidate
    return None
