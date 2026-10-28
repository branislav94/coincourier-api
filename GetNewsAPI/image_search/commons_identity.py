"""Conservative Commons identities shared by providers and reuse checks.

Page IDs are strongest; filenames are case-sensitive aliases.  Raw values are
preserved by ``normalize_commons_identity`` but never treated as verified Commons
identities by ``commons_identities``.
"""
from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit


COMMONS_SOURCES = frozenset({"wikimedia", "wikimedia_commons"})
COMMONS_PAGE_HOSTS = frozenset({"commons.wikimedia.org", "commons.m.wikimedia.org"})
COMMONS_FILE_HOSTS = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org"})
_PAGE_KEY = re.compile(r"wikimedia:([1-9][0-9]*)\Z")
_FILE_PATH = re.compile(r"/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/]+)(?:/[^/]+)?\Z")
_TEXT_URL = re.compile(r"(?:https?://|(?<![:/])//)[^\s<>\"']+", re.IGNORECASE)


def _page_id(value: object) -> str:
    if isinstance(value, bool):
        return ""
    text = str(value or "")
    return str(int(text)) if re.fullmatch(r"[0-9]+", text) and int(text) > 0 else ""


def _file_title(value: str) -> str:
    # This function accepts decoded titles, including persisted aliases. Only
    # URL path callers decode percent escapes, exactly once.
    title = unicodedata.normalize("NFC", value).replace("_", " ").strip()
    if any(ord(char) < 32 for char in title):
        return ""
    if title[:5].lower() == "file:":
        title = title[5:]
    title = re.sub(r"\s+", " ", title).strip()
    if not title or any(char in title for char in "/\\#"):
        return ""
    return title


def _decode_url_title(value: str) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return ""
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        return ""


def commons_identities(
    *values: object,
    source: str = "",
    foreign_identifier: object = "",
) -> set[str]:
    """Return verified page/file aliases; never infer an origin from a filename."""
    identities: set[str] = set()
    trusted_source = str(source or "").strip().lower() in COMMONS_SOURCES
    if trusted_source and (pageid := _page_id(foreign_identifier)):
        identities.add(f"wikimedia:{pageid}")
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        # Provider JSON and persisted aliases are already decoded. HTML decoding
        # belongs only at the WordPress text boundary below.
        value = raw.strip()
        if match := _PAGE_KEY.fullmatch(value):
            identities.add(f"wikimedia:{match.group(1)}")
            continue
        if value.startswith("wikimedia:file:"):
            if title := _file_title(value[len("wikimedia:file:"):]):
                identities.add(f"wikimedia:file:{title}")
            continue
        if trusted_source and value[:5].lower() == "file:":
            if title := _file_title(value):
                identities.add(f"wikimedia:file:{title}")
            continue
        try:
            parsed = urlsplit("https:" + value if value.startswith("//") else value)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                continue
            if parsed.port not in {None, 80, 443}:
                continue
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path + parsed.query):
            continue
        if host in COMMONS_PAGE_HOSTS:
            try:
                params = parse_qs(parsed.query, errors="strict")
            except UnicodeDecodeError:
                continue
            if pageid := _page_id((params.get("curid") or [""])[0]):
                identities.add(f"wikimedia:{pageid}")
            title_value = ""
            if parsed.path.startswith("/wiki/"):
                title_value = _decode_url_title(parsed.path[len("/wiki/"):])
            elif parsed.path == "/w/index.php":
                title_value = (params.get("title") or [""])[0]
            if title_value[:5].lower() == "file:":
                if title := _file_title(title_value):
                    identities.add(f"wikimedia:file:{title}")
        elif host in COMMONS_FILE_HOSTS:
            if match := _FILE_PATH.fullmatch(parsed.path):
                if title := _file_title(_decode_url_title(match.group(1))):
                    identities.add(f"wikimedia:file:{title}")
    return identities


def normalize_commons_identity(
    *values: object,
    source: str = "",
    foreign_identifier: object = "",
) -> tuple[str, list[str]]:
    """Return the strongest Commons key and raw/normalized aliases for storage."""
    identities = commons_identities(*values, source=source, foreign_identifier=foreign_identifier)
    if _identity_conflict(identities):
        return "", []
    page_keys = sorted(value for value in identities if _PAGE_KEY.fullmatch(value))
    canonical = page_keys[0] if page_keys else (sorted(identities)[0] if identities else "")
    raw_aliases = {value.strip() for value in values if isinstance(value, str) and value.strip()}
    return canonical, sorted(raw_aliases | identities)


def _identity_conflict(identities: set[str]) -> bool:
    return (
        sum(bool(_PAGE_KEY.fullmatch(value)) for value in identities) > 1
        or sum(value.startswith("wikimedia:file:") for value in identities) > 1
    )


def commons_identity_conflict(
    *values: object,
    source: str = "",
    foreign_identifier: object = "",
) -> bool:
    """Reject contradictory IDs/titles before providers form identity aliases."""
    return _identity_conflict(commons_identities(*values, source=source, foreign_identifier=foreign_identifier))


def commons_identities_from_text(text: str) -> set[str]:
    """Extract Commons identities from old WordPress HTML/plain-text credits."""
    text = html.unescape(str(text or ""))
    values = _TEXT_URL.findall(text)
    non_url_text = _TEXT_URL.sub(" ", text)
    values.extend(re.findall(r"(?<![A-Za-z0-9_-])wikimedia:[1-9][0-9]*(?![A-Za-z0-9_-])", non_url_text))
    identities = commons_identities(*values)
    # A sentence delimiter is not part of a URL, while punctuation within a file
    # title must remain valid. Include both interpretations conservatively.
    identities.update(commons_identities(*(value.rstrip(".,;)") for value in values)))
    return identities
