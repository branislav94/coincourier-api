from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImageCandidate:
    provider: str
    asset_id: str = ""
    asset_key: str = ""
    image_url: str = ""
    download_url: str = ""
    thumbnail_url: str = ""
    source_page_url: str = ""
    canonical_source: str = ""
    creator_name: str = ""
    creator_url: str = ""
    license_name: str = ""
    license_version: str = ""
    license_url: str = ""
    attribution_text: str = ""
    width: int | None = None
    height: int | None = None
    orientation: str = ""
    mime_type: str = ""
    query: str = ""
    provider_rank: int = 0
    relevance_score: float = 0.0
    topic_score: float = 0.0
    quality_score: float = 0.0
    popularity_score: float = 0.0
    final_score: float = 0.0
    provider_threshold: float = 0.0
    url_hash: str = ""
    content_sha256: str = ""
    perceptual_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.provider = (self.provider or "").strip().lower()
        try:
            self.width = int(self.width) if self.width is not None else None
        except (TypeError, ValueError):
            self.width = None
        try:
            self.height = int(self.height) if self.height is not None else None
        except (TypeError, ValueError):
            self.height = None
        if not self.download_url:
            self.download_url = self.image_url
        if not self.image_url:
            self.image_url = self.download_url
        if not self.asset_key:
            if self.asset_id:
                self.asset_key = f"{self.provider}:{self.asset_id}"
            else:
                seed = self.download_url or self.source_page_url or self.canonical_source
                self.asset_key = f"{self.provider}:url:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"
        if not self.url_hash:
            self.url_hash = hashlib.sha256((self.download_url or self.image_url).encode("utf-8")).hexdigest()
        if not self.orientation and self.width and self.height:
            if self.width > self.height:
                self.orientation = "landscape"
            elif self.width < self.height:
                self.orientation = "portrait"
            else:
                self.orientation = "square"

    @property
    def usable_url(self) -> str:
        return self.download_url or self.image_url


@dataclass
class DownloadedImage:
    content: bytes
    mime_type: str
    width: int
    height: int
    content_sha256: str
    perceptual_hash: str


@dataclass
class ImageSearchResult:
    candidate: ImageCandidate | None = None
    downloaded: DownloadedImage | None = None
    providers_attempted: tuple[str, ...] = ()
    provider_failures: dict[str, str] = field(default_factory=dict)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    all_available_providers_exhausted: bool = False
