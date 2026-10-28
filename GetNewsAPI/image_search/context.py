"""Deterministic subject checks for crypto names with unrelated common meanings."""
from __future__ import annotations

import html
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .models import ImageCandidate
from .queries import article_blob, normalize_terms


# No existing shared project registry distinguishes these word senses. Aliases
# belong to one concept, so SOL/Solana cannot count as two independent matches.
PROJECT_ALIASES = {
    "solana": frozenset({"solana", "sol"}),
    "avalanche": frozenset({"avalanche", "avax"}),
    "polygon": frozenset({"polygon", "matic", "pol"}),
    "near": frozenset({"near"}),
    "stacks": frozenset({"stacks", "stx"}),
    "optimism": frozenset({"optimism", "op"}),
    "arbitrum": frozenset({"arbitrum", "arb"}),
    "injective": frozenset({"injective", "inj"}),
    "cosmos": frozenset({"cosmos", "atom"}),
    "stellar": frozenset({"stellar", "xlm"}),
    "flow": frozenset({"flow"}),
}
DISTINCT_PROJECT_TICKERS = frozenset({"avax", "matic", "stx", "xlm"})
ORDINARY_WORD_PROJECTS = frozenset({"near", "stacks", "optimism", "flow"})
UNRELATED_CONTEXT = {
    "solana": frozenset({"beach", "california", "diego", "symphony", "quartet", "musician", "musicians", "trumpet", "brass", "concert"}),
    "avalanche": frozenset({"snow", "snowfall", "mountain", "mountains", "ski", "skiing", "alpine", "glacier", "landslide"}),
    "polygon": frozenset({"geometry", "geometric", "triangle", "triangles", "vertices", "angles", "shape", "shapes", "pentagon", "hexagon"}),
    "near": frozenset({"nearby", "distance", "proximity", "location"}),
    "stacks": frozenset({"books", "library", "chimney", "chimneys", "smoke"}),
    "optimism": frozenset({"happiness", "hopeful", "emotion", "emotions", "smile", "smiling", "psychology"}),
    "arbitrum": frozenset({"arbitration", "legal", "court"}),
    "injective": frozenset({"mathematics", "mathematical", "function", "functions", "surjective", "bijection"}),
    "cosmos": frozenset({"galaxy", "galaxies", "astronomy", "nebula", "telescope", "flowers", "flower"}),
    "stellar": frozenset({"astronomy", "stars", "star", "galaxy", "telescope"}),
    "flow": frozenset({"river", "waterfall", "fluid", "fluids", "plumbing"}),
}
STRONG_CRYPTO_CONTEXT = frozenset({
    "blockchain", "blockchains", "cryptocurrency", "cryptocurrencies", "crypto", "defi",
    "bitcoin", "btc", "ethereum", "eth", "web3", "stablecoin", "stablecoins",
    "tokenized", "tokenization", "tokenisation", "staking", "etf", "nft", "nfts",
})
SUPPORTING_CONTEXT = frozenset({
    "token", "tokens", "coin", "coins", "wallet", "wallets", "validator", "validators",
    "network", "protocol", "exchange", "market", "markets", "finance", "financial",
    "trading", "digital", "asset", "assets", "bank", "banking", "fund", "funds",
    "money", "currency", "currencies", "capital", "investment", "investments",
    "logo", "brand", "branding",
})
GENERIC_CONCEPTS = frozenset({
    "new", "news", "latest", "today", "update", "updates", "brings", "bring", "launch",
    "launches", "launched", "announce", "announces", "announced", "announcement", "plans",
    "report", "reports", "said", "grows", "growth", "rise", "rises", "fall", "falls",
    "high", "low", "big", "major", "another", "first", "second", "year", "month", "day",
    "project", "projects", "company", "companies", "world", "global", "future", "next",
    "program", "system", "platform", "development", "event", "people",
    "file", "image", "photo", "photograph", "jpg", "jpeg", "png", "webp", "svg",
})
ALL_PROJECT_ALIASES = frozenset().union(*PROJECT_ALIASES.values())
SUBJECT_FIELDS = ("title", "description", "image_description", "object_name", "tags", "alt", "source_page_title")
ARTICLE_FIELDS = ("seo_focus", "title", "hashtags", "category", "tickers", "source_name")
_URL = re.compile(r"(?:https?://|//|www\.)[^\s<>\"']+", re.I)


class _SubjectText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.suppressed += 1
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.suppressed = max(0, self.suppressed - 1)
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _plain_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " ".join(_plain_text(item.get("name") if isinstance(item, dict) else item) for item in value)
    if not isinstance(value, str):
        return ""
    parser = _SubjectText()
    parser.feed(html.unescape(value))
    parser.close()
    return _URL.sub(" ", " ".join(parser.parts))


def image_article_context(article: dict[str, Any]) -> dict[str, str]:
    """Retain only fields already used for image relevance, never article bodies."""
    return {key: str(article.get(key) or "") for key in ARTICLE_FIELDS}


def _candidate_subject(candidate: ImageCandidate) -> tuple[set[str], set[str], str]:
    explicit = " ".join(_plain_text(candidate.metadata.get(key)) for key in SUBJECT_FIELDS)
    aggregate = _plain_text(candidate.metadata.get("metadata_text"))
    # Aggregated provider metadata also includes artist/source provenance. It
    # cannot corroborate the depicted subject; explicit subject fields stay intact.
    aggregate = " ".join(re.findall(r"[a-z0-9]+", aggregate.lower()))
    removed: set[str] = set()
    for value in (candidate.creator_name, candidate.metadata.get("source"), candidate.metadata.get("provider")):
        provenance = " ".join(re.findall(r"[a-z0-9]+", _plain_text(value).lower()))
        if provenance and provenance not in removed:
            matches = list(re.finditer(r"(?<!\w)" + re.escape(provenance) + r"(?!\w)", aggregate))
            if matches:
                last = matches[-1]
                aggregate = aggregate[:last.start()] + " " + aggregate[last.end():]
            removed.add(provenance)
    hints: list[str] = []
    for url in (candidate.source_page_url, candidate.usable_url):
        try:
            # Filenames may reveal a collision but never provide positive crypto
            # evidence. Hostnames, user accounts and query strings are not subjects.
            hints.append(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))
        except (ValueError, UnicodeError, TypeError):
            continue
    subject_text = explicit + " " + aggregate
    return set(normalize_terms(subject_text)), set(normalize_terms(subject_text + " " + " ".join(hints))), subject_text


def context_rejection_reason(candidate: ImageCandidate, article: dict[str, Any]) -> str:
    """Reject single ambiguous-name matches without independent subject context."""
    article_terms = set(normalize_terms(article_blob(article)))
    requested = article_terms | set(normalize_terms(candidate.query))
    subject, anchors, subject_text = _candidate_subject(candidate)
    article_crypto = bool(article_terms & (STRONG_CRYPTO_CONTEXT | SUPPORTING_CONTEXT))
    requested_crypto = bool(requested & (STRONG_CRYPTO_CONTEXT | SUPPORTING_CONTEXT))
    tickers = set(normalize_terms(str(article.get("tickers") or "")))
    subject_request = set(normalize_terms(article_blob({**article, "source_name": ""}))) | set(normalize_terms(candidate.query))
    second_concepts = subject_request - ALL_PROJECT_ALIASES - GENERIC_CONCEPTS - SUPPORTING_CONTEXT
    second_concepts = {term for term in second_concepts if len(term) >= 4 and not term.isdigit()}
    matched = []
    for project, aliases in PROJECT_ALIASES.items():
        if not requested & aliases or not anchors & aliases:
            continue
        unrelated = UNRELATED_CONTEXT[project]
        if not article_crypto and article_terms & unrelated:
            continue  # An explicitly geographic/non-crypto article keeps its sense.
        if project in ORDINARY_WORD_PROJECTS and not requested_crypto and not tickers & aliases:
            continue
        matched.append((project, aliases, unrelated))
    if not matched:
        return ""
    if subject & STRONG_CRYPTO_CONTEXT:
        return ""
    if re.search(r"\bdigital\s+(?:assets?|currenc(?:y|ies))\b", subject_text, re.I):
        return ""
    for _project, _aliases, unrelated in matched:
        second_concepts -= unrelated
    if subject & second_concepts:
        return ""
    for _project, aliases, unrelated in matched:
        if subject & aliases & DISTINCT_PROJECT_TICKERS:
            return ""
        if not subject & unrelated and subject & aliases and subject & SUPPORTING_CONTEXT:
            return ""
    return "context_mismatch"
