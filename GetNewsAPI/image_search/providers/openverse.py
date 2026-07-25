from __future__ import annotations

import logging
from typing import Any

from config import (
    IMAGE_SEARCH_RESULTS_PER_QUERY,
    OPENVERSE_CLIENT_ID,
    OPENVERSE_CLIENT_SECRET,
    OPENVERSE_ENABLED,
    OPENVERSE_MIN_SCORE,
    OPENVERSE_PER_PAGE,
)

from ..cache import CachedHttpClient
from ..models import ImageCandidate
from ..provider import ImageSearchProvider, ProviderUnavailable


logger = logging.getLogger(__name__)
OPENVERSE_SEARCH_URL = "https://api.openverse.org/v1/images/"
OPENVERSE_TOKEN_URL = "https://api.openverse.org/v1/auth_tokens/token/"


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
        self._authentication_attempted = False

    def _headers(self) -> dict[str, str]:
        if self._authentication_attempted:
            return {"Authorization": f"Bearer {self._access_token}"} if self._access_token else {}
        self._authentication_attempted = True
        if not self.client_id or not self.client_secret:
            return {}
        try:
            payload = self.client.post_json(
                self.provider_name,
                OPENVERSE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            self._access_token = str(payload.get("access_token") or "") or None
        except ProviderUnavailable:
            logger.warning("[IMG-V2] Openverse authentication unavailable; using anonymous search")
        return {"Authorization": f"Bearer {self._access_token}"} if self._access_token else {}

    def search(self, query: str) -> list[ImageCandidate]:
        if not self.enabled:
            return []
        data = self.client.get_json(
            self.provider_name,
            query,
            OPENVERSE_SEARCH_URL,
            headers=self._headers(),
            params={
                "q": query,
                "page_size": self.per_page,
                "license": "cc0,pdm,by",
                "mature": "false",
            },
        )
        return [
            candidate
            for rank, raw in enumerate(data.get("results") or [])
            if (candidate := self.normalize_candidate(raw, query, rank)) is not None
        ]

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
        return ImageCandidate(
            provider=self.provider_name,
            asset_id=asset_id,
            image_url=str(image_url),
            thumbnail_url=str(raw.get("thumbnail") or ""),
            source_page_url=source_page,
            canonical_source=canonical_source,
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
