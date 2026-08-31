"""Generic publishing boundary shared by concrete CMS adapters."""

from __future__ import annotations

from typing import Protocol

from .models import PublicationArticle, PublicationContext, PublicationImage, PublicationResult


class Publisher(Protocol):
    def publish(
        self,
        article: PublicationArticle,
        image: PublicationImage | None,
        context: PublicationContext,
    ) -> PublicationResult:
        ...
