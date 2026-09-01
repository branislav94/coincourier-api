"""Minimal persistence and native nearest-neighbor queries for vector storage."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any, Callable, Sequence

from .db import connect_vector_db
from .models import (
    VECTOR_DIMENSIONS,
    SourceType,
    VectorChunkRecord,
    VectorDocumentDraft,
    VectorDocumentRecord,
    VectorMatch,
)


class VectorIdentityConflictError(RuntimeError):
    pass


def serialize_embedding(values: Sequence[float]) -> str:
    if len(values) != VECTOR_DIMENSIONS:
        raise ValueError(f"embedding must contain exactly {VECTOR_DIMENSIONS} values")
    normalized = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite numbers")
        normalized.append(number)
    return json.dumps(normalized, separators=(",", ":"), allow_nan=False)


class VectorRepository:
    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or connect_vector_db

    def upsert_document(self, document: VectorDocumentDraft) -> int:
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            cursor.execute(
                """
                INSERT INTO vector_documents
                    (document_key, source_type, source_article_id, rich_article_id,
                     source_url, title, published_at, content_hash, content_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    source_url = VALUES(source_url),
                    title = VALUES(title),
                    published_at = VALUES(published_at),
                    updated_at = UTC_TIMESTAMP()
                """,
                (
                    document.document_key,
                    document.source_type.value,
                    document.source_article_id,
                    document.rich_article_id,
                    document.source_url,
                    document.title,
                    document.published_at,
                    document.content_hash,
                    document.content_version,
                ),
            )
            document_id = int(cursor.lastrowid)
            cursor.execute(
                "SELECT content_hash FROM vector_documents WHERE id=%s FOR UPDATE",
                (document_id,),
            )
            stored_hash = cursor.fetchone()["content_hash"]
            if stored_hash != document.content_hash:
                raise VectorIdentityConflictError(
                    "document key/version already belongs to a different content hash"
                )
            connection.commit()
            return document_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_document(self, document_id: int) -> VectorDocumentRecord | None:
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, document_key, source_type, source_article_id,
                       rich_article_id, source_url, title, published_at,
                       content_hash, content_version
                FROM vector_documents WHERE id=%s
                """,
                (document_id,),
            )
            row = cursor.fetchone()
            return self._document_record(row) if row else None
        finally:
            cursor.close()
            connection.close()

    def upsert_chunk(
        self,
        *,
        document_id: int,
        chunk_index: int,
        chunk_text: str,
        embedding: Sequence[float],
        embedding_model: str,
        embedding_version: str,
    ) -> int:
        if document_id <= 0 or chunk_index < 0:
            raise ValueError("document_id must be positive and chunk_index nonnegative")
        if not chunk_text:
            raise ValueError("chunk_text is required")
        if not embedding_model.strip() or not embedding_version.strip():
            raise ValueError("embedding model and version are required")
        vector_text = serialize_embedding(embedding)
        chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()

        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO vector_chunks
                    (document_id, chunk_index, chunk_text, chunk_hash, embedding,
                     embedding_model, embedding_dimensions, embedding_version)
                VALUES (%s,%s,%s,%s,VEC_FromText(%s),%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    chunk_text = VALUES(chunk_text),
                    chunk_hash = VALUES(chunk_hash),
                    embedding = VALUES(embedding),
                    embedding_model = VALUES(embedding_model),
                    embedding_dimensions = VALUES(embedding_dimensions),
                    updated_at = UTC_TIMESTAMP()
                """,
                (
                    document_id,
                    chunk_index,
                    chunk_text,
                    chunk_hash,
                    vector_text,
                    embedding_model,
                    VECTOR_DIMENSIONS,
                    embedding_version,
                ),
            )
            chunk_id = int(cursor.lastrowid)
            connection.commit()
            return chunk_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_chunks(
        self,
        document_id: int,
        *,
        embedding_version: str | None = None,
    ) -> list[VectorChunkRecord]:
        clauses = ["document_id=%s"]
        params: list[Any] = [document_id]
        if embedding_version is not None:
            clauses.append("embedding_version=%s")
            params.append(embedding_version)

        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT id, document_id, chunk_index, chunk_text, chunk_hash,
                       VEC_ToText(embedding) AS embedding_text,
                       embedding_model, embedding_dimensions, embedding_version
                FROM vector_chunks
                WHERE {' AND '.join(clauses)}
                ORDER BY embedding_version, chunk_index, id
                """,
                tuple(params),
            )
            return [
                VectorChunkRecord(
                    id=int(row["id"]),
                    document_id=int(row["document_id"]),
                    chunk_index=int(row["chunk_index"]),
                    chunk_text=row["chunk_text"],
                    chunk_hash=row["chunk_hash"],
                    embedding=tuple(float(value) for value in json.loads(row["embedding_text"])),
                    embedding_model=row["embedding_model"],
                    embedding_dimensions=int(row["embedding_dimensions"]),
                    embedding_version=row["embedding_version"],
                )
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()
            connection.close()

    def nearest_chunks(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        embedding_version: str,
        source_type: SourceType | None = None,
        published_after: datetime | None = None,
    ) -> list[VectorMatch]:
        if top_k <= 0:
            return []
        if not embedding_version.strip():
            raise ValueError("embedding_version is required")
        vector_text = serialize_embedding(query_embedding)
        clauses = ["c.embedding_version=%s"]
        params: list[Any] = [vector_text, embedding_version]
        if source_type is not None:
            clauses.append("d.source_type=%s")
            params.append(source_type.value)
        if published_after is not None:
            clauses.append("d.published_at >= %s")
            params.append(published_after)
        params.append(min(top_k, 100))

        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT STRAIGHT_JOIN
                    VEC_DISTANCE_COSINE(c.embedding, VEC_FromText(%s)) AS distance,
                    d.id AS document_id, d.document_key, d.source_type,
                    d.source_article_id, d.rich_article_id, d.source_url,
                    d.title, d.published_at,
                    c.id AS chunk_id, c.chunk_index, c.chunk_text, c.chunk_hash,
                    c.embedding_model, c.embedding_version
                FROM vector_chunks c
                JOIN vector_documents d ON d.id = c.document_id
                WHERE {' AND '.join(clauses)}
                ORDER BY distance ASC, c.id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            return [self._match(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def enqueue_embedding_job(self, document_id: int, embedding_version: str) -> int:
        if document_id <= 0 or not embedding_version.strip():
            raise ValueError("document_id and embedding_version are required")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO embedding_jobs (document_id, embedding_version)
                VALUES (%s,%s)
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id), updated_at = UTC_TIMESTAMP()
                """,
                (document_id, embedding_version),
            )
            job_id = int(cursor.lastrowid)
            connection.commit()
            return job_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _document_record(row: dict[str, Any]) -> VectorDocumentRecord:
        return VectorDocumentRecord(
            id=int(row["id"]),
            document_key=row["document_key"],
            source_type=SourceType(row["source_type"]),
            source_article_id=int(row["source_article_id"]),
            rich_article_id=int(row["rich_article_id"]) if row["rich_article_id"] else None,
            source_url=row["source_url"],
            title=row["title"],
            published_at=row["published_at"],
            content_hash=row["content_hash"],
            content_version=row["content_version"],
        )

    @staticmethod
    def _match(row: dict[str, Any]) -> VectorMatch:
        return VectorMatch(
            distance=float(row["distance"]),
            document_id=int(row["document_id"]),
            document_key=row["document_key"],
            source_type=SourceType(row["source_type"]),
            source_article_id=int(row["source_article_id"]),
            rich_article_id=int(row["rich_article_id"]) if row["rich_article_id"] else None,
            source_url=row["source_url"],
            title=row["title"],
            published_at=row["published_at"],
            chunk_id=int(row["chunk_id"]),
            chunk_index=int(row["chunk_index"]),
            chunk_text=row["chunk_text"],
            chunk_hash=row["chunk_hash"],
            embedding_model=row["embedding_model"],
            embedding_version=row["embedding_version"],
        )
