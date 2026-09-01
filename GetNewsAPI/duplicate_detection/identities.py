"""Conservative deterministic identities for source articles."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_PARAMETERS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
    }
)
_WHITESPACE_RE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True)
class ArticleIdentity:
    provider_article_id: str | None
    event_id: str | None
    canonical_url: str
    normalized_title: str
    title_fingerprint: str | None
    content_fingerprint: str | None


def _clean_identifier(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.casefold()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMETERS


def canonicalize_url(value: Any) -> str:
    """Normalize only URL features that are safe identity variants."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return raw.split("#", 1)[0]

    scheme = parsed.scheme.casefold()
    if hostname is None:
        netloc = parsed.netloc.casefold()
    else:
        host = hostname.casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        userinfo = ""
        if parsed.username is not None:
            userinfo = parsed.username
            if parsed.password is not None:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        netloc = f"{userinfo}{host}"

    path = parsed.path
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path[:-1]

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept_pairs = [(name, item) for name, item in query_pairs if not _is_tracking_parameter(name)]
    query = parsed.query if len(kept_pairs) == len(query_pairs) else urlencode(kept_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_title(value: Any) -> str:
    """Normalize title presentation without removing words, dates, or numbers."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = "".join(
        " " if unicodedata.category(character)[0] in {"P", "Z"} else character
        for character in normalized
    )
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def title_fingerprint(value: Any) -> str | None:
    normalized = normalize_title(value)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_source_content(value: Any) -> str:
    raw = html.unescape(str(value or ""))
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = " ".join(parser.parts)
    except (TypeError, ValueError):
        text = raw
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def content_fingerprint(value: Any, *, minimum_characters: int = 80) -> str | None:
    normalized = normalize_source_content(value)
    if len(normalized) < minimum_characters:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_article_identity(article: Mapping[str, Any]) -> ArticleIdentity:
    source_content = article.get("text") or article.get("full_text") or ""
    source_url = article.get("news_url") or article.get("canonical_url") or ""
    return ArticleIdentity(
        provider_article_id=_clean_identifier(
            article.get("news_id") or article.get("provider_article_id")
        ),
        event_id=_clean_identifier(article.get("event_id") or article.get("eventid")),
        canonical_url=canonicalize_url(source_url),
        normalized_title=normalize_title(article.get("title")),
        title_fingerprint=title_fingerprint(article.get("title")),
        content_fingerprint=content_fingerprint(source_content),
    )
