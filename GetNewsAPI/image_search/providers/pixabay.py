from __future__ import annotations

from typing import Any

from config import (
    IMAGE_SEARCH_RESULTS_PER_QUERY,
    PIXABAY_API_KEY,
    PIXABAY_ENABLED,
    PIXABAY_MIN_SCORE,
    PIXABAY_ORIENTATION,
    PIXABAY_PER_PAGE,
)

from ..cache import CachedHttpClient
from ..models import ImageCandidate
from ..provider import ImageSearchProvider


PIXABAY_SEARCH_URL = "https://pixabay.com/api/"


class PixabayProvider(ImageSearchProvider):
    provider_name = "pixabay"

    def __init__(
        self,
        *,
        client: CachedHttpClient | None = None,
        api_key: str | None = PIXABAY_API_KEY,
        enabled: bool = PIXABAY_ENABLED,
        per_page: int = min(PIXABAY_PER_PAGE, IMAGE_SEARCH_RESULTS_PER_QUERY),
        threshold: float = PIXABAY_MIN_SCORE,
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
            PIXABAY_SEARCH_URL,
            params={
                "key": str(self.api_key),
                "q": query,
                "image_type": "photo",
                "orientation": PIXABAY_ORIENTATION,
                "safesearch": "true",
                "per_page": self.per_page,
            },
        )
        return [
            candidate
            for rank, raw in enumerate(data.get("hits") or [])
            if (candidate := self.normalize_candidate(raw, query, rank)) is not None
        ]

    def normalize_candidate(self, raw: dict[str, Any], query: str, provider_rank: int) -> ImageCandidate | None:
        image_url = raw.get("largeImageURL") or raw.get("webformatURL")
        source_page = str(raw.get("pageURL") or "")
        creator = str(raw.get("user") or "")
        if not image_url or not source_page:
            return None
        creator_url = ""
        if creator and raw.get("user_id"):
            creator_url = f"https://pixabay.com/users/{creator}-{raw['user_id']}/"
        metadata_text = " ".join(str(raw.get(key) or "") for key in ("tags", "pageURL", "user"))
        return ImageCandidate(
            provider=self.provider_name,
            asset_id=str(raw.get("id") or ""),
            image_url=str(image_url),
            source_page_url=source_page,
            canonical_source=f"pixabay:{raw.get('id')}" if raw.get("id") else source_page,
            creator_name=creator,
            creator_url=creator_url,
            license_name="pixabay-content-license",
            license_url="https://pixabay.com/service/license-summary/",
            attribution_text=f"Image by {creator} on Pixabay: {source_page}",
            width=raw.get("imageWidth"),
            height=raw.get("imageHeight"),
            query=query,
            provider_rank=provider_rank,
            provider_threshold=self.candidate_threshold,
            metadata={
                "metadata_text": metadata_text,
                "views": raw.get("views"),
                "downloads": raw.get("downloads"),
                "likes": raw.get("likes"),
            },
        )
