"""Small target-neutral models used by the current publishing boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class PublicationArticle:
    title: str
    html_content: str
    categories: str = "General"
    tags: str = ""
    slug_candidate: str | None = None
    seo_focus: str = ""
    seo_description: str = ""
    canonical_url: str | None = None
    schema_jsonld: str | None = None

    @classmethod
    def from_mapping(cls, article: Mapping[str, Any]) -> "PublicationArticle":
        """Map the current rich-article row without adding new semantics."""
        return cls(
            title=article["title"],
            html_content=article.get("full_text"),
            categories=article.get("category", "General"),
            tags=article.get("hashtags", ""),
            slug_candidate=article.get("seo_slug"),
            seo_focus=article.get("seo_focus", ""),
            seo_description=article.get("seo_meta", ""),
            canonical_url=article.get("seo_canonical"),
            schema_jsonld=article.get("schema_jsonld"),
        )


@dataclass(frozen=True)
class PublicationImage:
    external_id: int
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PublicationContext:
    published_at_utc: datetime
    publication_key: str | None = None
    raw_article_id: int | None = None
    rich_article_id: int | None = None
    source_url: str | None = None
    existing_external_id: int | None = None
    persist_external_state: Callable[[int, str | None], None] | None = None


@dataclass(frozen=True)
class PublicationResult:
    success: bool
    external_id: int | None = None
    external_url: str | None = None
    media_external_id: int | None = None
    created: bool = False
    reconciled: bool = False
    error: str | None = None
