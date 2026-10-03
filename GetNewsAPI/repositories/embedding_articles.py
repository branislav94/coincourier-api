"""Read-only application article access for vector ingestion and workers."""

from __future__ import annotations

from typing import Any, Callable

import mysql.connector

from config import DB_CONFIG
from embeddings.ingestion import ApplicationArticle
from vector_store.models import SourceType, VectorDocumentRecord


class EmbeddingArticleRepository:
    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or (lambda: mysql.connector.connect(**DB_CONFIG))

    def check_connection(self) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            cursor.close()
            connection.close()

    def scan_recent(self, limit: int) -> list[ApplicationArticle]:
        if limit <= 0:
            return []
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT source_type, source_article_id, rich_article_id,
                       source_url, title, body, published_at
                FROM (
                    SELECT 'source_article' AS source_type,
                           c.id AS source_article_id,
                           NULL AS rich_article_id,
                           c.news_url AS source_url,
                           c.title, c.full_text AS body,
                           c.publish_date AS published_at,
                           COALESCE(c.selected_at, c.insertDate) AS sort_at,
                           0 AS source_priority, c.id AS article_id
                    FROM cryptonewsapi c
                    WHERE c.chosen_for_publish = 1
                      AND c.full_text IS NOT NULL
                      AND TRIM(c.full_text) <> ''
                    UNION ALL
                    SELECT 'coincourier_generated' AS source_type,
                           c.id AS source_article_id,
                           r.id AS rich_article_id,
                           r.news_url AS source_url,
                           r.title, r.full_text AS body,
                           r.publish_date AS published_at,
                           COALESCE(r.publish_date, r.insertDate) AS sort_at,
                           1 AS source_priority, r.id AS article_id
                    FROM rich_crpytonews r
                    LEFT JOIN cryptonewsapi c ON c.id = r.raw_article_id
                    WHERE r.full_text IS NOT NULL
                      AND TRIM(r.full_text) <> ''
                ) candidates
                ORDER BY sort_at DESC, source_priority ASC, article_id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [self._article(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def max_article_id(self, source_type: SourceType) -> int:
        table = self._table_for(source_type)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            return int((cursor.fetchone() or (0,))[0] or 0)
        finally:
            cursor.close()
            connection.close()

    def scan_backfill_page(
        self,
        source_type: SourceType,
        *,
        before_id: int,
        page_size: int,
    ) -> list[ApplicationArticle]:
        if before_id <= 0 or page_size <= 0:
            return []
        if source_type is SourceType.SOURCE_ARTICLE:
            sql = """
                SELECT 'source_article' AS source_type,
                       c.id AS source_article_id, NULL AS rich_article_id,
                       c.news_url AS source_url, c.title,
                       c.full_text AS body, c.publish_date AS published_at
                FROM cryptonewsapi c
                WHERE c.id < %s
                  AND c.full_text IS NOT NULL
                  AND TRIM(c.full_text) <> ''
                ORDER BY c.id DESC
                LIMIT %s
            """
        elif source_type is SourceType.COINCOURIER_GENERATED:
            sql = """
                SELECT 'coincourier_generated' AS source_type,
                       c.id AS source_article_id, r.id AS rich_article_id,
                       r.news_url AS source_url, r.title,
                       r.full_text AS body, r.publish_date AS published_at
                FROM rich_crpytonews r
                LEFT JOIN cryptonewsapi c ON c.id = r.raw_article_id
                WHERE r.id < %s
                  AND r.full_text IS NOT NULL
                  AND TRIM(r.full_text) <> ''
                ORDER BY r.id DESC
                LIMIT %s
            """
        else:
            raise ValueError(f"unsupported backfill source type: {source_type}")

        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(sql, (before_id, page_size))
            return [self._article(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def load_body(self, document: VectorDocumentRecord) -> str | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            if document.source_type is SourceType.SOURCE_ARTICLE:
                cursor.execute(
                    "SELECT full_text FROM cryptonewsapi WHERE id=%s",
                    (document.source_article_id,),
                )
            elif document.source_type is SourceType.COINCOURIER_GENERATED:
                cursor.execute(
                    """
                    SELECT r.full_text
                    FROM rich_crpytonews r
                    JOIN cryptonewsapi c ON c.id = r.raw_article_id
                    WHERE r.id=%s AND c.id=%s
                    """,
                    (document.rich_article_id, document.source_article_id),
                )
            else:
                raise ValueError(f"unsupported vector source type: {document.source_type}")
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _table_for(source_type: SourceType) -> str:
        if source_type is SourceType.SOURCE_ARTICLE:
            return "cryptonewsapi"
        if source_type is SourceType.COINCOURIER_GENERATED:
            return "rich_crpytonews"
        raise ValueError(f"unsupported backfill source type: {source_type}")

    @staticmethod
    def _article(row: dict[str, Any]) -> ApplicationArticle:
        return ApplicationArticle(
            source_type=SourceType(row["source_type"]),
            source_article_id=(
                int(row["source_article_id"])
                if row.get("source_article_id") is not None
                else None
            ),
            rich_article_id=(
                int(row["rich_article_id"])
                if row.get("rich_article_id") is not None
                else None
            ),
            source_url=row.get("source_url"),
            title=row.get("title") or "",
            body=row.get("body") or "",
            published_at=row.get("published_at"),
        )
