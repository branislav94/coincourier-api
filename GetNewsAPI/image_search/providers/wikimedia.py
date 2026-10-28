from __future__ import annotations

import html
from html.parser import HTMLParser
import math
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from config import IMAGE_SEARCH_RESULTS_PER_QUERY, WIKIMEDIA_ENABLED, WIKIMEDIA_MIN_SCORE, WIKIMEDIA_PER_PAGE

from ..cache import CachedHttpClient
from ..commons_identity import COMMONS_PAGE_HOSTS, commons_identities, commons_identity_conflict, normalize_commons_identity
from ..downloader import IMAGE_SEARCH_USER_AGENT, MAX_IMAGE_BYTES
from ..models import ImageCandidate
from ..provider import ImageSearchProvider, ProviderUnavailable


WIKIMEDIA_SEARCH_URL = "https://commons.wikimedia.org/w/api.php"
WIKIMEDIA_USER_AGENT = IMAGE_SEARCH_USER_AGENT
EXTMETADATA_FIELDS = (
    "Artist|Credit|Attribution|AttributionRequired|LicenseShortName|LicenseUrl|UsageTerms|"
    "Copyrighted|NonFree|ObjectName|ImageDescription|Restrictions|DeletionReason"
)
SUPPORTED_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
ORIGINAL_TRACKING_PARAMETERS = frozenset({"utm_source", "utm_campaign", "utm_content"})
_VERSIONS = r"(?:1\.0|2\.0|2\.5|3\.0|4\.0)"
_CC_BY_LABEL = re.compile(rf"(?:CC BY|Creative Commons Attribution) ({_VERSIONS})(?: International)?", re.I)
_URL_SUFFIX = r"(?:/(?:deed(?:\.[A-Za-z-]+)?|legalcode(?:\.[A-Za-z-]+)?))?/?"


class _MetadataText(HTMLParser):
    """Retain readable notices and safe links without retaining executable HTML."""

    def __init__(self, *, preserve_links: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0
        self.links: list[str] = []
        self.preserve_links = preserve_links

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
            try:
                url = urlsplit(href)
                valid = url.scheme in {"http", "https"} and bool(url.hostname) and not url.username and not url.password
            except ValueError:
                valid = False
            self.links.append(href if valid else "")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.suppressed = max(0, self.suppressed - 1)
            return
        if self.suppressed:
            return
        if tag == "a" and self.links:
            href = self.links.pop()
            if href and self.preserve_links:
                self.parts.append(f" ({href}) ")
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _text(value: object, *, preserve_links: bool = True) -> str:
    parser = _MetadataText(preserve_links=preserve_links)
    parser.feed(str(value or ""))
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _metadata_value(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    return str(value.get("value") or "") if isinstance(value, dict) else ""


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value or "")):
        return None
    result = int(str(value))
    return result if result > 0 else None


def _valid_original_url(value: str) -> bool:
    """Accept original Commons files and only the API's observed tracking keys."""
    try:
        parts = urlsplit(value)
        if re.search(r"%(?![0-9A-Fa-f]{2})", parts.query):
            return False
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, errors="strict")
        return bool(
            parts.scheme in {"http", "https"} and parts.hostname == "upload.wikimedia.org"
            and not parts.username and not parts.password and parts.port in {None, 80, 443}
            and re.fullmatch(r"/wikipedia/commons/[0-9a-f]/[0-9a-f]{2}/[^/]+", parts.path)
            and all(key in ORIGINAL_TRACKING_PARAMETERS for key, _ in query)
            and not parts.fragment
        )
    except (ValueError, UnicodeDecodeError):
        return False


def _license_label(value: str) -> tuple[str, str] | None:
    label = re.sub(r"\s+", " ", value).strip()
    if label.lower() in {"cc0", "cc0 1.0", "cc0 1.0 universal", "creative commons cc0 1.0 universal"}:
        return "cc0", "1.0"
    if label.lower() in {"public domain", "public domain mark", "public domain mark 1.0", "pdm", "pdm 1.0"}:
        return "pdm", "1.0"
    if match := _CC_BY_LABEL.fullmatch(label):
        return "cc-by", match.group(1)
    return None


def _parse_license(label: str, url: str, usage_terms: str = "") -> tuple[str, str, str] | None:
    recognized = _license_label(label)
    if not recognized:
        return None
    if usage_terms and _license_label(usage_terms) != recognized:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.hostname not in {"creativecommons.org", "www.creativecommons.org"}
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
            or parsed.query or parsed.fragment
        ):
            return None
    except ValueError:
        return None
    name, version = recognized
    path = {"cc0": "/publicdomain/zero/1.0", "pdm": "/publicdomain/mark/1.0"}.get(name)
    if name == "cc-by":
        path = f"/licenses/by/{version}"
    if not re.fullmatch(re.escape(str(path)) + _URL_SUFFIX, parsed.path):
        return None
    return name, version, f"https://creativecommons.org{path}/"


class WikimediaProvider(ImageSearchProvider):
    provider_name = "wikimedia"

    def __init__(
        self,
        *,
        client: CachedHttpClient | None = None,
        enabled: bool = WIKIMEDIA_ENABLED,
        per_page: int = min(WIKIMEDIA_PER_PAGE, IMAGE_SEARCH_RESULTS_PER_QUERY),
        threshold: float = WIKIMEDIA_MIN_SCORE,
    ) -> None:
        self.client = client or CachedHttpClient()
        self.enabled = enabled
        self.per_page = max(1, min(per_page, IMAGE_SEARCH_RESULTS_PER_QUERY, 50))
        self.candidate_threshold = threshold
        self.last_search_stats: dict[str, int] = {"raw": 0, "normalized": 0}
        self.last_search_rejections: dict[str, int] = {}

    @staticmethod
    def _validate_payload(data: dict[str, Any]) -> bool:
        if not isinstance(data, dict) or "error" in data or "errors" in data:
            return False
        if "query" not in data:
            # MediaWiki emits no query object for a definitive empty generator.
            return data.get("batchcomplete") is True and not data.get("warnings") and "continue" not in data
        query = data.get("query")
        if not isinstance(query, dict) or not isinstance(query.get("pages"), list):
            return False
        for page in query["pages"]:
            if (
                not isinstance(page, dict) or not _positive_int(page.get("pageid"))
                or page.get("ns") != 6 or not isinstance(page.get("title"), str)
                or not page["title"].startswith("File:")
            ):
                return False
            if "imageinfo" in page and (
                not isinstance(page["imageinfo"], list)
                or any(not isinstance(info, dict) for info in page["imageinfo"])
            ):
                return False
        return True

    def _rejected(self, reason: str) -> None:
        self.last_search_rejections[reason] = self.last_search_rejections.get(reason, 0) + 1

    @staticmethod
    def _payload_retry_delay(data: dict[str, Any]) -> float | None:
        """Retry only MediaWiki's recognized temporary failures, never raw info."""
        error = data.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        if code == "ratelimited":
            return 5.0
        if code != "maxlag":
            return None
        try:
            lag = float(error.get("lag", 5.0))
        except (TypeError, ValueError, OverflowError):
            return 5.0
        return min(30.0, max(5.0, lag)) if math.isfinite(lag) else 5.0

    def search(self, query: str) -> list[ImageCandidate]:
        self.last_search_stats = {"raw": 0, "normalized": 0}
        self.last_search_rejections = {}
        if not self.enabled:
            return []
        data = self.client.get_json(
            self.provider_name, query, WIKIMEDIA_SEARCH_URL,
            headers={"User-Agent": WIKIMEDIA_USER_AGENT},
            params={
                "action": "query", "format": "json", "formatversion": 2,
                "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
                "gsrlimit": self.per_page, "prop": "imageinfo",
                "iiprop": "url|size|mime|canonicaltitle|extmetadata", "iilimit": 1,
                "iiextmetadatalanguage": "en", "iiextmetadatafilter": EXTMETADATA_FIELDS,
                "maxlag": 5,
            },
            payload_validator=self._validate_payload,
            payload_retry_delay=self._payload_retry_delay,
        )
        # Also validate injected/mock clients, without depending on their internals.
        if not self._validate_payload(data):
            raise ProviderUnavailable(self.provider_name, "invalid_response")
        pages = (data.get("query") or {}).get("pages") or []
        self.last_search_stats["raw"] = len(pages)
        pages = sorted(enumerate(pages), key=lambda pair: (_positive_int(pair[1].get("index")) or pair[0] + 1, pair[0]))
        candidates = [
            item for rank, (_, page) in enumerate(pages)
            if (item := self.normalize_candidate(page, query, rank)) is not None
        ]
        self.last_search_stats["normalized"] = len(candidates)
        return candidates

    def normalize_candidate(self, raw: dict[str, Any], query: str, provider_rank: int) -> ImageCandidate | None:
        pageid = _positive_int(raw.get("pageid"))
        title = str(raw.get("title") or "")
        infos = raw.get("imageinfo")
        if not pageid:
            self._rejected("missing_page_id")
            return None
        if raw.get("ns") != 6:
            self._rejected("invalid_namespace")
            return None
        if not title.startswith("File:"):
            self._rejected("invalid_file_title")
            return None
        if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
            self._rejected("missing_imageinfo")
            return None
        info = infos[0]
        canonical_title = ""
        if info.get("canonicaltitle"):
            canonical_title = str(info["canonicaltitle"])
            if not canonical_title.startswith("File:"):
                self._rejected("invalid_canonical_title")
                return None
        mime = str(info.get("mime") or "").lower()
        if mime not in SUPPORTED_MIMES:
            self._rejected("unsupported_format")
            return None
        size = _positive_int(info.get("size"))
        if not size or size > MAX_IMAGE_BYTES:
            self._rejected("invalid_size")
            return None
        image_url = str(info.get("url") or "")
        if not _valid_original_url(image_url):
            self._rejected("invalid_original_url")
            return None
        if not commons_identities(image_url):
            self._rejected("unrecognized_commons_identity")
            return None
        metadata = info.get("extmetadata")
        if not isinstance(metadata, dict):
            self._rejected("license_rejected")
            return None
        if any(len(_metadata_value(metadata, key)) > 24000 for key in ("Artist", "Credit", "Attribution")):
            self._rejected("license_rejected")
            return None
        fields = {key: _text(_metadata_value(metadata, key)) for key in EXTMETADATA_FIELDS.split("|")}
        if (
            fields["NonFree"].lower() not in {"", "false", "no", "0"}
            or fields["Restrictions"] or fields["DeletionReason"]
        ):
            self._rejected("license_rejected")
            return None
        license_info = _parse_license(fields["LicenseShortName"], fields["LicenseUrl"], fields["UsageTerms"])
        if not license_info:
            self._rejected("license_rejected")
            return None
        license_name, license_version, license_url = license_info
        copyright_flag = fields["Copyrighted"].lower()
        if (license_name in {"cc0", "pdm"} and copyright_flag not in {"", "false", "no", "0"}) or (license_name == "cc-by" and copyright_flag in {"false", "no", "0"}):
            self._rejected("license_rejected")
            return None
        artist, credit, custom = fields["Artist"], fields["Credit"], fields["Attribution"]
        artist_name = _text(_metadata_value(metadata, "Artist"), preserve_links=False)
        if license_name == "cc-by" and artist_name.lower() in {"", "unknown", "unknown creator", "n/a", "none"}:
            self._rejected("license_rejected")
            return None
        source_page = str(info.get("descriptionurl") or "")
        try:
            source_parts = urlsplit(source_page)
            source_page_valid = source_parts.hostname in COMMONS_PAGE_HOSTS and bool(commons_identities(source_page))
        except ValueError:
            source_page_valid = False
        if not source_page_valid:
            source_page = f"https://commons.wikimedia.org/wiki/{quote(title.replace(' ', '_'), safe=':()')}"
        width, height = _positive_int(info.get("width")), _positive_int(info.get("height"))
        if not width or not height:
            self._rejected("dimensions_rejected")
            return None
        identity_values = (
            f"wikimedia:{pageid}", title, canonical_title, source_page,
            str(info.get("descriptionshorturl") or ""), image_url,
        )
        if commons_identity_conflict(*identity_values, source="wikimedia", foreign_identifier=pageid):
            self._rejected("commons_identity_conflict")
            return None
        canonical, aliases = normalize_commons_identity(*identity_values, source="wikimedia", foreign_identifier=pageid)
        label = {"cc-by": "CC BY", "cc0": "CC0", "pdm": "PDM"}[license_name]
        parts = [_text(title)]
        parts.extend(f"{name}: {value}." for name, value in (("Artist", artist), ("Credit", credit), ("Attribution", custom)) if value)
        parts.extend((f"Source: {source_page}.", f"License: {label} {license_version} ({license_url})."))
        return ImageCandidate(
            provider=self.provider_name, asset_id=str(pageid), asset_key=f"wikimedia:{pageid}",
            image_url=image_url, source_page_url=source_page, canonical_source=canonical,
            identity_aliases=aliases, creator_name=artist, license_name=license_name,
            license_version=license_version, license_url=license_url, attribution_text=html.escape(" ".join(parts), quote=False),
            width=width, height=height, mime_type=mime, query=query, provider_rank=provider_rank,
            provider_threshold=self.candidate_threshold,
            metadata={
                "metadata_text": " ".join((title, fields["ObjectName"][:4000], fields["ImageDescription"][:4000])),
                "title": title, "credit": credit, "attribution_complete": True,
                "attribution_authoritative": False,
            },
        )
