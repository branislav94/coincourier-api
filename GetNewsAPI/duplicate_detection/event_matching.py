"""Lightweight deterministic extraction of event facts."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_ASSET_ALIASES = {
    "bitcoin": "asset:BTC",
    "btc": "asset:BTC",
    "ethereum": "asset:ETH",
    "ether": "asset:ETH",
    "eth": "asset:ETH",
    "solana": "asset:SOL",
    "sol": "asset:SOL",
    "xrp": "asset:XRP",
    "bnb": "asset:BNB",
    "tether": "asset:USDT",
    "usdt": "asset:USDT",
}
_ORGANIZATION_ALIASES = {
    "blackrock": "org:blackrock",
    "binance": "org:binance",
    "circle": "org:circle",
    "coinbase": "org:coinbase",
    "federal reserve": "org:federal-reserve",
    "kraken": "org:kraken",
    "ripple": "org:ripple",
    "securities and exchange commission": "org:sec",
    "sec": "org:sec",
}
_ACTION_PATTERNS = {
    "acquire": r"\b(?:acquire[sd]?|acquisition)\b",
    "approve": r"\b(?:approve[sd]?|approval)\b",
    "file": r"\b(?:file[sd]?|filing)\b",
    "halt": r"\b(?:halt(?:ed|s|ing)?|suspend(?:ed|s|ing)?)\b",
    "launch": r"\b(?:launch(?:ed|es|ing)?)\b",
    "reject": r"\b(?:reject(?:ed|s|ing|ion)?)\b",
    "report": r"\b(?:report(?:ed|s|ing)?)\b",
    "resume": r"\b(?:resume[sd]?|resumption)\b",
    "sue": r"\b(?:sue[sd]?|suing|lawsuit)\b",
}
_ISO_DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")
_MONTH_DATE_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}(?:,?\s+(?:19|20)\d{2})?\b"
)
_PERCENT_RE = re.compile(r"(?<!\w)[+-]?\d+(?:\.\d+)?\s*%")
_MONEY_RE = re.compile(
    r"(?<!\w)(?:[$\u20ac\u00a3]\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion|trillion))?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:million|billion|trillion)\s*(?:dollars?|euros?|pounds?))\b"
)
_QUANTITY_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:btc|eth|tokens?|coins?|users?|accounts?|transactions?)\b"
)
_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True)
class EventFacts:
    entities: tuple[str, ...]
    dates: tuple[str, ...]
    numbers: tuple[str, ...]
    actions: tuple[str, ...]

    @property
    def key_entities(self) -> frozenset[str]:
        return frozenset(item for item in self.entities if not item.startswith("source:"))


def _normalized_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _normalize_date(value: Any) -> str:
    cleaned = _normalized_text(value).replace(",", "").replace("/", "-")
    iso_match = re.fullmatch(r"((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})", cleaned)
    if iso_match:
        year, month, day = (int(part) for part in iso_match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    parts = cleaned.split()
    if len(parts) in {2, 3} and parts[0][:3] in _MONTH_NUMBERS:
        month = _MONTH_NUMBERS[parts[0][:3]]
        day = int(parts[1])
        if len(parts) == 3:
            return f"{int(parts[2]):04d}-{month:02d}-{day:02d}"
        return f"{month:02d}-{day:02d}"
    return cleaned


def _normalize_number(value: Any) -> str:
    cleaned = _normalized_text(value).replace(",", "")
    cleaned = re.sub(r"([$\u20ac\u00a3])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+%", "%", cleaned)
    return cleaned


def _structured_values(value: Any) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return (str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def _matches_alias(text: str, alias: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text) is not None


def extract_event_facts(article: Mapping[str, Any]) -> EventFacts:
    title = _normalized_text(article.get("title"))
    source_content = _normalized_text(article.get("text") or article.get("full_text"))
    combined = f"{title} {source_content}".strip()

    entities: set[str] = set()
    for ticker in _structured_values(article.get("tickers")):
        entities.add(f"asset:{ticker.upper()}")
    for alias, identity in _ASSET_ALIASES.items():
        if _matches_alias(combined, alias):
            entities.add(identity)
    for alias, identity in _ORGANIZATION_ALIASES.items():
        if _matches_alias(combined, alias):
            entities.add(identity)
    source_name = _normalized_text(article.get("source_name"))
    if source_name:
        entities.add(f"source:{source_name}")

    dates = {_normalize_date(match) for match in _ISO_DATE_RE.findall(combined)}
    dates.update(_normalize_date(match) for match in _MONTH_DATE_RE.findall(combined))

    numbers = {_normalize_number(match) for match in _PERCENT_RE.findall(combined)}
    numbers.update(_normalize_number(match) for match in _MONEY_RE.findall(combined))
    numbers.update(_normalize_number(match) for match in _QUANTITY_RE.findall(combined))

    actions = {
        action
        for action, pattern in _ACTION_PATTERNS.items()
        if re.search(pattern, combined)
    }
    return EventFacts(
        entities=tuple(sorted(entities)),
        dates=tuple(sorted(dates)),
        numbers=tuple(sorted(numbers)),
        actions=tuple(sorted(actions)),
    )
