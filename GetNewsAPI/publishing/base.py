"""Generic publishing boundary shared by concrete CMS adapters."""

from __future__ import annotations

from typing import Protocol

from .models import PublicationArticle, PublicationContext, PublicationImage, PublicationResult


class Publisher(Protocol):
    def reconcile(
        self,
        article: PublicationArticle,
        context: PublicationContext,
    ) -> PublicationResult | None:
        ...

    def publish(
        self,
        article: PublicationArticle,
        image: PublicationImage | None,
        context: PublicationContext,
    ) -> PublicationResult:
        ...

    def external_media_exists(self, external_id: int) -> bool:
        ...

    def find_external_media(self, publication_key: str) -> int | None:
        ...

    def persist_external_media_identity(self, external_id: int, publication_key: str) -> None:
        ...
