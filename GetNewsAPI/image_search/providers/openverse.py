from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from config import (
    IMAGE_SEARCH_RESULTS_PER_QUERY,
    OPENVERSE_ACCESS_TOKEN,
    OPENVERSE_API_BASE_URL,
    OPENVERSE_CLIENT_ID,
    OPENVERSE_CLIENT_SECRET,
    OPENVERSE_ENABLED,
    OPENVERSE_HTTPS_PROXY,
    OPENVERSE_MIN_SCORE,
    OPENVERSE_PER_PAGE,
    OPENVERSE_TOKEN_URL,
)

from ..cache import CachedHttpClient
from ..commons_identity import commons_identity_conflict, normalize_commons_identity
from ..models import ImageCandidate
from ..provider import ImageSearchProvider, ProviderUnavailable


logger = logging.getLogger(__name__)
OPENVERSE_SEARCH_URL = "https://api.openverse.org/v1/images/"


def _validated_endpoint(value: str, *, proxy: bool = False) -> str:
    """Allow approved HTTP transport settings without URL-carried credentials."""
    try:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        allowed_schemes = {"http", "https"} if proxy else {"https"}
        if (
            parsed.scheme not in allowed_schemes or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or any(character.isspace() or ord(character) < 32 for character in normalized)
            or (proxy and parsed.path not in {"", "/"})
        ):
            raise ValueError
        parsed.port  # Validate the port without retaining its raw failure message.
        return normalized
    except Exception:
        raise ProviderUnavailable("openverse", "invalid_configuration") from None


def _authentication_failure(error: ProviderUnavailable) -> ProviderUnavailable:
    reason = (
        "authentication_failed"
        if error.reason == "http_error" and error.status_code in {400, 401, 403}
        else error.reason
    )
    return ProviderUnavailable(
        "openverse", reason, status_code=error.status_code,
        retry_exhausted=error.retry_exhausted, content_type=error.content_type,
        server_cloudflare=error.server_cloudflare, cf_ray_present=error.cf_ray_present,
    )


def _license_label(name: str, version: str) -> str:
    labels = {"cc-by": "CC BY", "cc0": "CC0", "pdm": "PDM"}
    label = labels.get(name, name.upper())
    return f"{label} {version}".strip()


def _known_creator(value: str) -> bool:
    return bool(value and value.strip().lower() not in {"unknown", "unknown creator", "n/a", "none"})


def _authoritative_attribution_is_complete(
    attribution: str,
    *,
    source_page: str,
    license_name: str,
    license_url: str,
) -> bool:
    normalized = attribution.lower().replace("-", " ")
    license_text = _license_label(license_name, "").lower().replace("-", " ")
    return bool(
        attribution
        and source_page in attribution
        and license_url in attribution
        and license_text in normalized
    )


def _fallback_attribution(
    *,
    title: str,
    creator: str,
    source_page: str,
    license_name: str,
    license_version: str,
    license_url: str,
) -> str:
    creator_text = f" by {creator}" if _known_creator(creator) else ""
    return (
        f"{title}{creator_text}. Source: {source_page}. "
        f"License: {_license_label(license_name, license_version)} ({license_url})"
    )


class OpenverseProvider(ImageSearchProvider):
    provider_name = "openverse"

    def __init__(
        self,
        *,
        client: CachedHttpClient | None = None,
        client_id: str | None = OPENVERSE_CLIENT_ID,
        client_secret: str | None = OPENVERSE_CLIENT_SECRET,
        api_base_url: str = OPENVERSE_API_BASE_URL,
        token_url: str = OPENVERSE_TOKEN_URL,
        https_proxy: str = OPENVERSE_HTTPS_PROXY,
        access_token: str | None = OPENVERSE_ACCESS_TOKEN,
        enabled: bool = OPENVERSE_ENABLED,
        per_page: int = min(OPENVERSE_PER_PAGE, IMAGE_SEARCH_RESULTS_PER_QUERY),
        threshold: float = OPENVERSE_MIN_SCORE,
    ) -> None:
        self.client = client or CachedHttpClient()
        self.client_id = client_id
        self.client_secret = client_secret
        self.enabled = enabled
        self.per_page = per_page
        self.candidate_threshold = threshold
        self._access_token: str | None = None
        self._configured_access_token = access_token
        self._authentication_attempted = False
        self._authentication_failure: ProviderUnavailable | None = None
        self._configuration_failure: ProviderUnavailable | None = None
        self.api_base_url = ""
        self.token_url = ""
        self.search_url = ""
        # Explicit per-request values prevent ambient proxies from silently
        # changing the Openverse route. Never mutate an injected/shared session.
        self.request_proxies = {"http": "", "https": "", "all": ""}
        try:
            self.api_base_url = _validated_endpoint(api_base_url).rstrip("/") + "/"
            self.token_url = _validated_endpoint(token_url)
            self.search_url = self.api_base_url + "images/"
            if https_proxy:
                self.request_proxies["https"] = _validated_endpoint(https_proxy, proxy=True)
        except ProviderUnavailable as exc:
            self._configuration_failure = exc

    def _headers(self) -> dict[str, str]:
        if self._configuration_failure is not None:
            raise self._configuration_failure
        if self._authentication_failure is not None:
            raise self._authentication_failure
        if self._authentication_attempted:
            return {"Authorization": f"Bearer {self._access_token}"} if self._access_token else {}
        self._authentication_attempted = True
        try:
            if self._configured_access_token:
                self._access_token = self._valid_token(self._configured_access_token)
                return {"Authorization": f"Bearer {self._access_token}"}
            if not self.client_id and not self.client_secret:
                return {}
            if not self.client_id or not self.client_secret:
                raise ProviderUnavailable(self.provider_name, "authentication_failed")
            payload = self.client.post_json(
                self.provider_name,
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                proxies=self.request_proxies,
                classify_edge_denials=True,
                allow_redirects=False,
            )
            if "error" in payload or "errors" in payload:
                raise ProviderUnavailable(self.provider_name, "authentication_failed")
            self._access_token = self._valid_token(payload.get("access_token"))
        except ProviderUnavailable as exc:
            self._authentication_failure = _authentication_failure(exc)
            raise self._authentication_failure from None
        return {"Authorization": f"Bearer {self._access_token}"}

    @staticmethod
    def _valid_token(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProviderUnavailable("openverse", "authentication_failed")
        token = value.strip()
        if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in token):
            raise ProviderUnavailable("openverse", "authentication_failed")
        return token

    def search(self, query: str) -> list[ImageCandidate]:
        if not self.enabled:
            return []
        data = self.client.get_json(
            self.provider_name,
            query,
            self.search_url,
            headers=self._headers(),
            params={
                "q": query,
                "page_size": self.per_page,
                "license": "cc0,pdm,by",
                "mature": "false",
            },
            payload_validator=lambda payload: self.valid_result_list(payload, "results"),
            proxies=self.request_proxies,
            classify_edge_denials=True,
            allow_redirects=False,
        )
        return self.normalize_results(data["results"], query)

    def normalize_candidate(self, raw: dict[str, Any], query: str, provider_rank: int) -> ImageCandidate | None:
        image_url = raw.get("url") or raw.get("thumbnail")
        source_page = str(raw.get("foreign_landing_url") or raw.get("detail_url") or "")
        if not image_url or not source_page:
            return None
        asset_id = str(raw.get("id") or raw.get("foreign_identifier") or "")
        source = str(raw.get("source") or raw.get("provider") or "openverse")
        foreign_id = str(raw.get("foreign_identifier") or "")
        creator = str(raw.get("creator") or "").strip()
        license_name = str(raw.get("license") or "").lower()
        if license_name == "by":
            license_name = "cc-by"
        license_version = str(raw.get("license_version") or "")
        license_url = str(raw.get("license_url") or "")
        title = str(raw.get("title") or "Untitled work").strip()
        raw_attribution = str(raw.get("attribution") or "").strip()
        authoritative_complete = _authoritative_attribution_is_complete(
            raw_attribution,
            source_page=source_page,
            license_name=license_name,
            license_url=license_url,
        )
        can_generate_attribution = license_name in {"cc0", "pdm"} or _known_creator(creator)
        if authoritative_complete:
            attribution = raw_attribution
        elif can_generate_attribution and license_url:
            attribution = _fallback_attribution(
                title=title,
                creator=creator,
                source_page=source_page,
                license_name=license_name,
                license_version=license_version,
                license_url=license_url,
            )
        else:
            attribution = ""
        tags = raw.get("tags") or []
        tag_text = " ".join(
            str(tag.get("name") if isinstance(tag, dict) else tag)
            for tag in tags
        )
        metadata_text = " ".join(
            [title, creator, source, tag_text]
        )
        canonical_source = f"{source}:{foreign_id}" if foreign_id else source_page
        identity_values = (
            f"{self.provider_name}:{asset_id}", canonical_source, source_page,
            str(image_url), str(raw.get("thumbnail") or ""),
        )
        if commons_identity_conflict(*identity_values, source=source, foreign_identifier=foreign_id):
            return None
        commons_source, identity_aliases = normalize_commons_identity(
            *identity_values, source=source, foreign_identifier=foreign_id,
        )
        if commons_source:
            canonical_source = commons_source
        else:
            identity_aliases = []
        return ImageCandidate(
            provider=self.provider_name,
            asset_id=asset_id,
            image_url=str(image_url),
            thumbnail_url=str(raw.get("thumbnail") or ""),
            source_page_url=source_page,
            canonical_source=canonical_source,
            identity_aliases=identity_aliases,
            creator_name=creator,
            creator_url=str(raw.get("creator_url") or ""),
            license_name=license_name,
            license_version=license_version,
            license_url=license_url,
            attribution_text=attribution,
            width=raw.get("width"),
            height=raw.get("height"),
            query=query,
            provider_rank=provider_rank,
            provider_threshold=self.candidate_threshold,
            metadata={
                "metadata_text": metadata_text,
                "foreign_identifier": foreign_id,
                "source": source,
                "provider": raw.get("provider"),
                "title": title,
                "tags": tags,
                "attribution_authoritative": authoritative_complete,
                "attribution_complete": bool(authoritative_complete or (attribution and can_generate_attribution)),
            },
        )
