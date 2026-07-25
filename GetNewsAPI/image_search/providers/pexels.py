from __future__ import annotations

from typing import Any

from config import (
    IMAGE_SEARCH_RESULTS_PER_QUERY,
    PEXELS_API_KEY,
    PEXELS_ENABLED,
    PEXELS_MIN_SCORE,
    PEXELS_ORIENTATION,
    PEXELS_PER_PAGE,
)

from ..cache import CachedHttpClient
from ..models import ImageCandidate
from ..provider import ImageSearchProvider


PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"


class PexelsProvider(ImageSearchProvider):
    provider_name = "pexels"

    def __init__(
        self,
        *,
        client: CachedHttpClient | None = None,
        api_key: str | None = PEXELS_API_KEY,
        enabled: bool = PEXELS_ENABLED,
        per_page: int = min(PEXELS_PER_PAGE, IMAGE_SEARCH_RESULTS_PER_QUERY),
        threshold: float = PEXELS_MIN_SCORE,
    ) -> None:
        self.client = client or CachedHttpClient()
        self.api_key = api_key
        self.enabled = enabled and bool(api_key)
        self.per_page = per_page
        self.candidate_threshold = threshold

    def search(self, query: str) -> list[ImageCandidate]:
        if not self.enabled:
            return []
        data = self.client.get_json(
            self.provider_name,
            query,
            PEXELS_SEARCH_URL,
            headers={"Authorization": str(self.api_key)},
            params={"query": query, "orientation": PEXELS_ORIENTATION, "per_page": self.per_page},
        )
        return [
            candidate
            for rank, raw in enumerate(data.get("photos") or [])
            if (candidate := self.normalize_candidate(raw, query, rank)) is not None
        ]

    def normalize_candidate(self, raw: dict[str, Any], query: str, provider_rank: int) -> ImageCandidate | None:
        source = raw.get("src") or {}
        image_url = source.get("large2x") or source.get("large") or source.get("original")
        source_page = str(raw.get("url") or "")
        creator = str(raw.get("photographer") or "")
        if not image_url or not source_page:
            return None
        metadata_text = " ".join(str(raw.get(key) or "") for key in ("alt", "url", "photographer"))
        return ImageCandidate(
            provider=self.provider_name,
            asset_id=str(raw.get("id") or ""),
            image_url=str(image_url),
            source_page_url=source_page,
            canonical_source=f"pexels:{raw.get('id')}" if raw.get("id") else source_page,
            creator_name=creator,
            creator_url=str(raw.get("photographer_url") or ""),
            license_name="pexels-license",
            license_url="https://www.pexels.com/license/",
            attribution_text=f"Photo by {creator} on Pexels: {source_page}",
            width=raw.get("width"),
            height=raw.get("height"),
            query=query,
            provider_rank=provider_rank,
            provider_threshold=self.candidate_threshold,
            metadata={"metadata_text": metadata_text, "liked": bool(raw.get("liked"))},
        )
