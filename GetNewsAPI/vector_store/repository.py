"""Minimal persistence and native nearest-neighbor queries for vector storage."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import datetime
from typing import Any, Callable, Sequence

import mysql.connector

from .db import connect_vector_db
from .models import (
    EmbeddingJobClaim,
    EmbeddingJobRecord,
    VECTOR_DIMENSIONS,
    SourceType,
    VectorChunkRecord,
    VectorDocumentDraft,
    VectorDocumentRecord,
    VectorMatch,
    VectorChunkWrite,
)


class VectorIdentityConflictError(RuntimeError):
    pass


MARIADB_ER_LOCK_DEADLOCK = 1213


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

    def claim_embedding_job(
        self,
        embedding_version: str,
        *,
        timeout_minutes: int,
    ) -> EmbeddingJobClaim | None:
        if not embedding_version.strip() or timeout_minutes <= 0:
            raise ValueError("embedding version and positive timeout are required")
        for attempt_index in range(2):
            try:
                return self._claim_embedding_job_once(
                    embedding_version,
                    timeout_minutes=timeout_minutes,
                )
            except mysql.connector.Error as error:
                if getattr(error, "errno", None) != MARIADB_ER_LOCK_DEADLOCK:
                    raise
                if attempt_index == 1:
                    return None
        return None

    def _claim_embedding_job_once(
        self,
        embedding_version: str,
        *,
        timeout_minutes: int,
    ) -> EmbeddingJobClaim | None:
        token = secrets.token_hex(32)
        connection = self._connect()
        cursor = None
        try:
            connection.start_transaction()
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT j.id AS job_id, j.document_id, j.embedding_version,
                       j.status AS job_status, j.attempt_count,
                       d.document_key, d.source_type, d.source_article_id,
                       d.rich_article_id, d.source_url, d.title, d.published_at,
                       d.content_hash, d.content_version
                FROM embedding_jobs j
                JOIN vector_documents d ON d.id = j.document_id
                WHERE j.embedding_version = %s
                  AND (
                    j.status IN ('pending', 'retryable')
                    OR (j.status = 'claimed'
                        AND (
                            j.claimed_at IS NULL
                            OR j.claimed_at < TIMESTAMPADD(
                                MINUTE, -%s, UTC_TIMESTAMP()
                            )
                        ))
                  )
                ORDER BY j.created_at ASC, j.id ASC
                LIMIT 1
                FOR UPDATE
                """,
                (embedding_version, timeout_minutes),
            )
            row = cursor.fetchone()
            if not row:
                connection.commit()
                return None

            cursor.execute(
                """
                UPDATE embedding_jobs
                SET status='claimed', claim_token=%s,
                    claimed_at=UTC_TIMESTAMP(),
                    attempt_count=attempt_count + 1,
                    last_error=NULL
                WHERE id=%s
                  AND embedding_version=%s
                  AND (
                    status IN ('pending', 'retryable')
                    OR (status='claimed'
                        AND (
                            claimed_at IS NULL
                            OR claimed_at < TIMESTAMPADD(
                                MINUTE, -%s, UTC_TIMESTAMP()
                            )
                        ))
                  )
                """,
                (token, row["job_id"], embedding_version, timeout_minutes),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            document = self._document_record(
                {
                    "id": row["document_id"],
                    "document_key": row["document_key"],
                    "source_type": row["source_type"],
                    "source_article_id": row["source_article_id"],
                    "rich_article_id": row["rich_article_id"],
                    "source_url": row["source_url"],
                    "title": row["title"],
                    "published_at": row["published_at"],
                    "content_hash": row["content_hash"],
                    "content_version": row["content_version"],
                }
            )
            return EmbeddingJobClaim(
                id=int(row["job_id"]),
                token=token,
                document=document,
                embedding_version=row["embedding_version"],
                attempt=int(row["attempt_count"] or 0) + 1,
                recovered=row["job_status"] == "claimed",
            )
        except BaseException:
            connection.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def get_embedding_job(self, job_id: int) -> EmbeddingJobRecord | None:
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, document_id, embedding_version, status,
                       attempt_count, claim_token, claimed_at, last_error
                FROM embedding_jobs WHERE id=%s
                """,
                (job_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return EmbeddingJobRecord(
                id=int(row["id"]),
                document_id=int(row["document_id"]),
                embedding_version=row["embedding_version"],
                status=row["status"],
                attempt_count=int(row["attempt_count"]),
                claim_token=row["claim_token"],
                claimed_at=row["claimed_at"],
                last_error=row["last_error"],
            )
        finally:
            cursor.close()
            connection.close()

    def complete_embedding_job_if_chunks_match(
        self,
        job_id: int,
        token: str,
        *,
        expected_chunk_hashes: Sequence[str],
        embedding_model: str,
        embedding_dimensions: int,
    ) -> bool | None:
        if not expected_chunk_hashes:
            return False
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            cursor.execute(
                """
                SELECT document_id, embedding_version
                FROM embedding_jobs
                WHERE id=%s AND status='claimed' AND claim_token=%s
                FOR UPDATE
                """,
                (job_id, token),
            )
            job = cursor.fetchone()
            if not job:
                connection.commit()
                return None
            cursor.execute(
                """
                SELECT chunk_index, chunk_hash, embedding_model,
                       embedding_dimensions
                FROM vector_chunks
                WHERE document_id=%s AND embedding_version=%s
                ORDER BY chunk_index ASC, id ASC
                """,
                (job["document_id"], job["embedding_version"]),
            )
            actual = [
                (
                    int(row["chunk_index"]),
                    row["chunk_hash"],
                    row["embedding_model"],
                    int(row["embedding_dimensions"]),
                )
                for row in cursor.fetchall()
            ]
            expected = [
                (index, chunk_hash, embedding_model, embedding_dimensions)
                for index, chunk_hash in enumerate(expected_chunk_hashes)
            ]
            if actual != expected:
                connection.commit()
                return False
            cursor.execute(
                """
                UPDATE embedding_jobs
                SET status='completed', claim_token=NULL, claimed_at=NULL,
                    last_error=NULL, updated_at=UTC_TIMESTAMP()
                WHERE id=%s AND status='claimed' AND claim_token=%s
                """,
                (job_id, token),
            )
            changed = cursor.rowcount == 1
            if changed:
                connection.commit()
            else:
                connection.rollback()
            return changed
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def persist_embedding_chunks_and_complete(
        self,
        job_id: int,
        token: str,
        *,
        chunks: Sequence[VectorChunkWrite],
        embedding_model: str,
        embedding_version: str,
    ) -> bool:
        if not chunks:
            raise ValueError("at least one embedding chunk is required")
        if [chunk.chunk_index for chunk in chunks] != list(range(len(chunks))):
            raise ValueError("embedding chunks must have contiguous indexes")

        rows = []
        for chunk in chunks:
            expected_hash = hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest()
            if chunk.chunk_hash != expected_hash:
                raise ValueError("embedding chunk hash does not match chunk text")
            rows.append(
                (
                    chunk.chunk_index,
                    chunk.chunk_text,
                    chunk.chunk_hash,
                    serialize_embedding(chunk.embedding),
                )
            )

        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            cursor.execute(
                """
                SELECT document_id, embedding_version
                FROM embedding_jobs
                WHERE id=%s AND status='claimed' AND claim_token=%s
                FOR UPDATE
                """,
                (job_id, token),
            )
            job = cursor.fetchone()
            if not job:
                connection.commit()
                return False
            if job["embedding_version"] != embedding_version:
                raise ValueError("claimed job embedding version changed")
            document_id = int(job["document_id"])

            cursor.execute(
                """
                DELETE FROM vector_chunks
                WHERE document_id=%s AND embedding_version=%s
                """,
                (document_id, embedding_version),
            )
            cursor.executemany(
                """
                INSERT INTO vector_chunks
                    (document_id, chunk_index, chunk_text, chunk_hash, embedding,
                     embedding_model, embedding_dimensions, embedding_version)
                VALUES (%s,%s,%s,%s,VEC_FromText(%s),%s,%s,%s)
                """,
                [
                    (
                        document_id,
                        chunk_index,
                        chunk_text,
                        chunk_hash,
                        vector_text,
                        embedding_model,
                        VECTOR_DIMENSIONS,
                        embedding_version,
                    )
                    for chunk_index, chunk_text, chunk_hash, vector_text in rows
                ],
            )
            cursor.execute(
                """
                UPDATE embedding_jobs
                SET status='completed', claim_token=NULL, claimed_at=NULL,
                    last_error=NULL, updated_at=UTC_TIMESTAMP()
                WHERE id=%s AND status='claimed' AND claim_token=%s
                """,
                (job_id, token),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def fail_embedding_job(
        self,
        job_id: int,
        token: str,
        safe_error: str,
        *,
        terminal: bool,
    ) -> bool:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE embedding_jobs
                SET status=%s, claim_token=NULL, claimed_at=NULL,
                    last_error=%s, updated_at=UTC_TIMESTAMP()
                WHERE id=%s AND status='claimed' AND claim_token=%s
                """,
                ("failed" if terminal else "retryable", safe_error[:500], job_id, token),
            )
            changed = cursor.rowcount == 1
            connection.commit()
            return changed
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
