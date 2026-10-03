"""Application-article registration for the optional vector subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from vector_store.models import SourceType, VectorDocumentDraft, VectorDocumentRecord

from .chunking import normalize_text, prepare_document
from .models import EmbeddingSettings
from .service import InvalidEmbeddingInput


@dataclass(frozen=True)
class ApplicationArticle:
    source_type: SourceType
    source_article_id: int | None
    rich_article_id: int | None
    source_url: str | None
    title: str
    body: str
    published_at: datetime | None

    @property
    def cursor_id(self) -> int:
        if self.source_type is SourceType.SOURCE_ARTICLE:
            return int(self.source_article_id or 0)
        return int(self.rich_article_id or 0)


@dataclass(frozen=True)
class RegistrationResult:
    status: str
    document_id: int | None = None
    job_id: int | None = None
    document_created: bool = False
    job_created: bool = False
    skip_reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.document_created or self.job_created


class RegistrationRepository(Protocol):
    def register_document(self, document: VectorDocumentDraft) -> tuple[int, bool]:
        ...

    def enqueue_embedding_job_with_status(
        self,
        document_id: int,
        embedding_version: str,
    ) -> tuple[int, bool]:
        ...


class ArticleContentRepository(Protocol):
    def load_body(self, document: VectorDocumentRecord) -> str | None:
        ...


class EmbeddingIngestionService:
    def __init__(
        self,
        *,
        vector_repository: RegistrationRepository,
        article_repository: ArticleContentRepository,
        settings: EmbeddingSettings,
    ) -> None:
        self.vector_repository = vector_repository
        self.article_repository = article_repository
        self.settings = settings

    def register(self, article: ApplicationArticle) -> RegistrationResult:
        if article.source_type is SourceType.SOURCE_ARTICLE:
            if not article.source_article_id or article.rich_article_id is not None:
                return RegistrationResult(
                    status="skipped",
                    skip_reason="invalid_source_provenance",
                )
        elif article.source_type is SourceType.COINCOURIER_GENERATED:
            if not article.source_article_id or not article.rich_article_id:
                return RegistrationResult(
                    status="skipped",
                    skip_reason="missing_generated_source_linkage",
                )
        else:
            return RegistrationResult(
                status="skipped",
                skip_reason="unsupported_source_type",
            )

        if not normalize_text(article.body):
            return RegistrationResult(status="skipped", skip_reason="empty_body")

        prepared = prepare_document(
            article.title,
            article.body,
            chunker_version=self.settings.chunker_version,
        )
        document_id, document_created = self.vector_repository.register_document(
            VectorDocumentDraft(
                source_type=article.source_type,
                source_article_id=int(article.source_article_id),
                rich_article_id=article.rich_article_id,
                source_url=article.source_url,
                title=article.title,
                published_at=article.published_at,
                content_hash=prepared.content_hash,
                content_version=prepared.content_version,
            )
        )
        job_id, job_created = self.vector_repository.enqueue_embedding_job_with_status(
            document_id,
            self.settings.embedding_version,
        )
        return RegistrationResult(
            status="registered" if document_created else "existing",
            document_id=document_id,
            job_id=job_id,
            document_created=document_created,
            job_created=job_created,
        )

    def load_content(self, document: VectorDocumentRecord) -> str:
        body = self.article_repository.load_body(document)
        if body is None or not normalize_text(body):
            raise InvalidEmbeddingInput("application article content is unavailable")
        return body
