"""Compact CC captions and a complete, safely encoded attachment audit trail.

CC BY 2.0/2.5/3.0 require supplied titles and credits (section 4(b)); CC BY
4.0 section 3(a) also retains supplied notices and modification information.
The caption always retains supplied titles, including for 4.0. Unknown custom
credit is preserved in full when compactly representable, otherwise rejected.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from image_search.license_policy import normalize_license_name
from image_search.providers.wikimedia import _parse_license

from .media import MediaMetadataError, validate_required_attribution


MAX_PUBLIC_TEXT_CHARS = 512
MAX_NOTICE_CHARS = 240
_URL = re.compile(r"(?:https?://|(?<![:/])//|www\.)[^\s<>\"']+", re.I)
_UNKNOWN = {"", "unknown", "unknown creator", "n/a", "none"}
_FIELD_LABEL = re.compile(r"\b(?:File|Artist|Credit|Attribution|Source|License|Changes):\s*", re.I)
_PUBLIC_UNSAFE = re.compile(r"[<>]|\b(?:href|rel)\s*=|\b(?:wikimedia|openverse|pexels|pixabay):|&(?:amp;)*(?:lt|gt);", re.I)


def _safe_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").rstrip(".").lower()
        if (
            parts.scheme not in {"http", "https"} or not host or parts.username is not None
            or parts.password is not None or parts.port not in {None, 80, 443}
            or len(value) > 8192 or any(char.isspace() or ord(char) < 32 for char in value)
            or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
            or re.search(r"%(?![0-9A-Fa-f]{2})", parts.path + parts.query)
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            labels = host.split(".")
            return "." in host and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels
            ) and not all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", part) for part in labels)
    except (TypeError, ValueError, UnicodeError):
        return False


class _ReadableText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.suppressed = 0
        self.anchor: tuple[str, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.suppressed += 1
        if self.suppressed:
            return
        self.parts.append(" ")
        if tag == "a":
            href = str(dict(attrs).get("href") or "")
            if href.startswith("//"):
                href = "https:" + href
            self.anchor = (href, len(self.parts)) if _safe_url(href) else None

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.suppressed = max(0, self.suppressed - 1)
            return
        if self.suppressed:
            return
        if tag == "a" and self.anchor is not None:
            href, start = self.anchor
            self.links.append((href, "".join(self.parts[start:]).strip()))
            self.anchor = None
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _read(value: object) -> tuple[str, list[tuple[str, str]]]:
    # Decode before parsing: HTMLParser alone can return encoded tags as literal
    # '<...>' data, which is unsuitable for a strictly plain-text caption.
    text = str(value or "")
    for _ in range(8):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    if html.unescape(text) != text:
        raise MediaMetadataError("attribution_missing")
    parser = _ReadableText()
    parser.feed(text)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip(), parser.links


def _plain(value: object) -> str:
    text, _links = _read(value)
    text = _URL.sub(lambda match: _url_token(match.group())[1], text)
    return re.sub(r"\s+", " ", re.sub(r"\(\s*\)", "", text)).strip(" .;·")


def _url_token(value: str) -> tuple[str, str]:
    url = value.rstrip(".,;")
    suffix = value[len(url):]
    while url.endswith(")") and url.count(")") > url.count("("):
        url, suffix = url[:-1], ")" + suffix
    return url, suffix


def _plain_caption_valid(value: str) -> bool:
    return not (_PUBLIC_UNSAFE.search(value) or _URL.search(value) or _FIELD_LABEL.search(value))


def _work_title(metadata: dict[str, Any]) -> str:
    supplied = metadata.get("work_title") or metadata.get("title")
    title, _links = _read(supplied)
    if _URL.search(title):
        raise MediaMetadataError("attribution_missing")
    if title.lower().startswith("file:"):
        title = title[5:].strip()
    if str(supplied or "").strip() and not title:
        raise MediaMetadataError("attribution_missing")
    return title


def _notice(value: object, *, structural_urls: set[str]) -> tuple[str, list[str]]:
    text, linked = _read(value)
    extra_links: list[str] = []

    def consume_url(match: re.Match) -> str:
        url, suffix = _url_token(match.group())
        if url.startswith("//"):
            url = "https:" + url
        elif url.lower().startswith("www."):
            url = "https://" + url
        if not _safe_url(url):
            raise MediaMetadataError("attribution_missing")
        if url not in structural_urls:
            extra_links.append(url)
        return suffix

    text = _URL.sub(consume_url, text)
    for url, _label in linked:
        if url not in structural_urls:
            extra_links.append(url)
    text = _FIELD_LABEL.sub("", text)
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s+", " ", text).strip(" .;·"), list(dict.fromkeys(extra_links))


def _creator_urls(metadata: dict[str, Any], source: str, license_url: str) -> list[str]:
    supplied = str(metadata.get("creator_url") or "")
    _text, urls = _notice(metadata.get("creator_name"), structural_urls={source, license_url})
    if supplied and _safe_url(supplied):
        urls.insert(0, supplied)
    return list(dict.fromkeys(urls))


def _public_domain_caption(metadata: dict[str, Any], source: str, license_url: str, label: str, modified: bool) -> str:
    """Optional public-domain credits may be omitted, never partially truncated."""
    try:
        title = _work_title(metadata)
    except MediaMetadataError:
        title = ""
    try:
        creator = _plain(metadata.get("creator_display_name") or metadata.get("creator_name") or metadata.get("credit_name"))
    except MediaMetadataError:
        creator = ""
    if len(title) > MAX_NOTICE_CHARS or not _plain_caption_valid(title):
        title = ""
    if creator.lower() in _UNKNOWN or len(creator) > MAX_NOTICE_CHARS or not _plain_caption_valid(creator):
        creator = ""
    try:
        creator_urls = _creator_urls(metadata, source, license_url)
    except MediaMetadataError:
        creator_urls = []
    provider = "Wikimedia Commons" if urlsplit(source).hostname in {"commons.wikimedia.org", "commons.m.wikimedia.org"} else "Openverse"

    def base() -> str:
        parts = ["Photo: " + creator if creator else "Photo"]
        if title:
            parts.extend((f"“{title}”", provider))
        else:
            parts.append(provider)
        parts.append(label)
        if modified:
            parts.append("Cropped")
        return " · ".join(parts)

    caption = base()
    if len(caption) > MAX_PUBLIC_TEXT_CHARS:
        title = ""
        caption = base()
    if len(caption) > MAX_PUBLIC_TEXT_CHARS:
        creator = ""
        caption = base()
    validate_required_attribution(caption)
    for value in (metadata.get("credit"), metadata.get("attribution_notice")):
        try:
            notice, links = _notice(value, structural_urls={source, license_url, *creator_urls})
        except MediaMetadataError:
            continue
        if not notice or notice.lower() in {"own work", creator.lower(), title.lower()} or len(notice) > MAX_NOTICE_CHARS:
            continue
        if links or not _plain_caption_valid(notice):
            continue
        proposed = caption + " · " + notice
        if len(proposed) <= MAX_PUBLIC_TEXT_CHARS:
            try:
                validate_required_attribution(proposed)
            except MediaMetadataError:
                continue
            caption = proposed
    return caption


def _full_description(full_audit: str, source: str, license_url: str, label: str, creator_urls: list[str]) -> str:
    """Retain standard URIs in the full public description, never the caption."""
    description = html.unescape(full_audit)
    present_urls = {_url_token(match.group())[0] for match in _URL.finditer(description)}
    for prefix, url in (("Source", source), ("License", license_url), *(("Creator URI", value) for value in creator_urls)):
        if url and url not in present_urls:
            value = f"{label} ({url})" if prefix == "License" else url
            description += f"\n{prefix}: {value}"
            present_urls.add(url)
    validate_required_attribution(description)
    description = html.escape(description, quote=False)
    validate_required_attribution(description)
    return description


def build_public_attribution(
    metadata: dict[str, Any], full_audit: str, *, modified: bool = True,
) -> tuple[str, str]:
    """Return a strictly plain caption and complete encoded description text.

    ``full_audit`` stays byte-for-byte intact in publication metadata. Description
    HTML escapes its once-decoded text so existing escaped Commons credits are
    not double encoded, and raw Openverse markup cannot execute in WordPress.
    """
    validate_required_attribution(full_audit)
    provider = str(metadata.get("provider") or "").lower()
    name = normalize_license_name(metadata.get("license_name"))
    version = str(metadata.get("license_version") or "")
    source = str(metadata.get("source_page_url") or metadata.get("credit_url") or "")
    license_url = str(metadata.get("license_url") or "")
    if provider not in {"openverse", "wikimedia"} or name not in {"cc-by", "cc0", "pdm"}:
        raise MediaMetadataError("attribution_missing")
    if not _safe_url(source) or not _safe_url(license_url):
        raise MediaMetadataError("attribution_missing")
    if provider == "wikimedia" and urlsplit(source).hostname not in {"commons.wikimedia.org", "commons.m.wikimedia.org"}:
        raise MediaMetadataError("attribution_missing")
    if name == "cc-by" and not version:
        match = re.fullmatch(r"/licenses/by/(1\.0|2\.0|2\.5|3\.0|4\.0)(?:/.*)?", urlsplit(license_url).path)
        version = match.group(1) if match else ""
    label = f"CC BY {version}" if name == "cc-by" else "CC0" if name == "cc0" else "PDM"
    parsed = _parse_license(label, license_url)
    if parsed is None:
        raise MediaMetadataError("attribution_missing")
    exact_label = f"CC BY {parsed[1]}" if name == "cc-by" else f"{label} {parsed[1]}"
    try:
        creator_urls = _creator_urls(metadata, source, license_url)
    except MediaMetadataError:
        if name == "cc-by":
            raise
        creator_urls = []
    description = _full_description(full_audit, source, license_url, exact_label, creator_urls)
    if name in {"cc0", "pdm"}:
        return _public_domain_caption(metadata, source, license_url, exact_label, modified), description
    title = _work_title(metadata)
    creator = _plain(metadata.get("creator_display_name") or metadata.get("creator_name") or metadata.get("credit_name"))
    if creator.lower() in _UNKNOWN:
        creator = ""
    structural_urls = {source, license_url, *creator_urls}
    notices: list[str] = []
    for value in (metadata.get("credit"), metadata.get("attribution_notice")):
        notice, links = _notice(value, structural_urls=structural_urls)
        if links:
            # Custom credit URIs cannot be silently removed or replaced with an
            # unusable anchor in a plain-text public credit. Try another asset.
            raise MediaMetadataError("attribution_missing")
        if notice and notice.lower() not in {"own work", creator.lower(), title.lower()}:
            if notice not in _notice(full_audit, structural_urls=structural_urls)[0]:
                raise MediaMetadataError("attribution_missing")
            notices.append(notice)
    if metadata.get("attribution_authoritative"):
        original = metadata.get("provider_attribution_text") or full_audit
        core, links = _notice(original, structural_urls=structural_urls)
        if links:
            raise MediaMetadataError("attribution_missing")
        core = re.sub(r"\b(?:is\s+)?licensed(?:\s+under)?\b", "", core, flags=re.I)
        core = re.sub(re.escape(exact_label) + r"\b", "", core, flags=re.I)
        core = re.sub(r"\s+", " ", core).strip(" .;·")
        simple = re.sub(r"[^\w]+", " ", core).strip().lower()
        expected = {re.sub(r"[^\w]+", " ", value).strip().lower() for value in (f"{title} by {creator}", f"{title} {creator}", creator)}
        if core and simple not in expected:
            if creator:
                notices.append(core)
            else:
                explicit_credit = re.search(r"\b(?:by|attribution|credit|copyright)\b|©", core, re.I)
                explicit_credit = explicit_credit or re.search(r"\b(?:Attribution|Credit):", _plain(original), re.I)
                if not explicit_credit:
                    raise MediaMetadataError("attribution_missing")
                creator = core
    if name == "cc-by" and not creator:
        raise MediaMetadataError("attribution_missing")
    if any(len(notice) > MAX_NOTICE_CHARS for notice in notices) or len(creator) > MAX_NOTICE_CHARS:
        raise MediaMetadataError("attribution_too_large")
    parts = ["Photo: " + creator] if creator else ["Photo"]
    if title:
        parts.append(f"“{title}”")
    provider_label = "Wikimedia Commons" if urlsplit(source).hostname in {"commons.wikimedia.org", "commons.m.wikimedia.org"} else "Openverse"
    parts.append(provider_label)
    parts.append(exact_label)
    if modified:
        parts.append("Cropped")
    parts.extend(dict.fromkeys(notices))
    caption = " · ".join(parts)
    if len(caption) > MAX_PUBLIC_TEXT_CHARS or not _plain_caption_valid(caption):
        raise MediaMetadataError("attribution_too_large")
    validate_required_attribution(caption)
    return caption, description
