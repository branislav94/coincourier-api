"""Short, idempotent transactions for durable semantic shadow evidence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from .db import connect_vector_db


class SemanticAssessmentStatus(str, Enum):
    DISABLED = "disabled"
    NOT_READY = "not_ready"
    NO_CANDIDATES = "no_candidates"
    RETRIEVED = "retrieved"
    ERROR = "error"


@dataclass(frozen=True)
class SemanticAssessmentDraft:
    query_source_article_id: int
    query_document_id: int | None
    embedding_version: str
    semantic_version: str
    status: SemanticAssessmentStatus
    reason: str | None
    lookback_hours: int
    requested_top_k: int
    query_chunks_available: int = 0
    query_chunks_considered: int = 0
    best_native_distance: float | None = None
    evidence: tuple[dict[str, Any], ...] = ()
    error_type: str | None = None
    safe_error: str | None = None

    def __post_init__(self) -> None:
        if self.query_source_article_id <= 0:
            raise ValueError("query_source_article_id must be positive")
        if self.query_document_id is not None and self.query_document_id <= 0:
            raise ValueError("query_document_id must be positive when present")
        if not self.embedding_version.strip() or len(self.embedding_version) > 191:
            raise ValueError("embedding_version is required and must fit storage")
        if not self.semantic_version.strip() or len(self.semantic_version) > 64:
            raise ValueError("semantic_version is required and must fit storage")
        if not 1 <= self.lookback_hours <= 24 * 365:
            raise ValueError("lookback_hours must be between 1 and 8760")
        if not 1 <= self.requested_top_k <= 20:
            raise ValueError("requested_top_k must be between 1 and 20")
        if self.query_chunks_available < 0:
            raise ValueError("query_chunks_available cannot be negative")
        if not 0 <= self.query_chunks_considered <= 8:
            raise ValueError("query_chunks_considered must be between 0 and 8")
        if len(self.evidence) > self.requested_top_k or len(self.evidence) > 20:
            raise ValueError("semantic evidence exceeds the requested top-K bound")
        if self.reason is not None and len(self.reason) > 191:
            raise ValueError("semantic reason must fit storage")
        if self.error_type is not None and len(self.error_type) > 128:
            raise ValueError("semantic error_type must fit storage")
        if self.safe_error is not None and len(self.safe_error) > 500:
            raise ValueError("semantic safe_error must fit storage")

        if self.status is SemanticAssessmentStatus.RETRIEVED:
            if not self.evidence or self.best_native_distance is None:
                raise ValueError("retrieved assessments require candidate evidence")
            if not math.isfinite(self.best_native_distance):
                raise ValueError("best native distance must be finite")
        elif self.evidence or self.best_native_distance is not None:
            raise ValueError("non-retrieved assessments cannot carry candidate evidence")

        if self.status is SemanticAssessmentStatus.ERROR:
            if not self.error_type or not self.safe_error:
                raise ValueError("error assessments require bounded error fields")
        elif self.error_type is not None or self.safe_error is not None:
            raise ValueError("only error assessments can carry error fields")

        candidate_ids: set[int] = set()
        native_distances: list[float] = []
        prohibited_fields = {
            "article_body",
            "chunk_text",
            "embedding",
            "full_text",
            "vector",
        }
        for candidate in self.evidence:
            if not isinstance(candidate, dict):
                raise ValueError("semantic candidate evidence must be an object")
            if prohibited_fields.intersection(candidate):
                raise ValueError("semantic evidence cannot contain bodies, chunks, or vectors")
            if candidate.get("source_type") != "source_article":
                raise ValueError("semantic evidence candidates must be source articles")
            candidate_id = int(candidate.get("candidate_source_article_id", 0))
            document_id = int(candidate.get("candidate_document_id", 0))
            if candidate_id <= 0 or document_id <= 0:
                raise ValueError("semantic candidate identities must be positive")
            if candidate_id == self.query_source_article_id:
                raise ValueError("same-source semantic evidence is not allowed")
            if candidate_id in candidate_ids:
                raise ValueError("semantic candidate source identities must be unique")
            candidate_ids.add(candidate_id)
            if candidate.get("embedding_version") != self.embedding_version:
                raise ValueError("semantic candidate embedding version mismatch")
            distance = float(candidate.get("native_distance", float("nan")))
            if not math.isfinite(distance):
                raise ValueError("semantic candidate distance must be finite")
            native_distances.append(distance)
        if native_distances and self.best_native_distance != min(native_distances):
            raise ValueError("best native distance must match candidate evidence")


@dataclass(frozen=True)
class SemanticAssessmentRecord:
    id: int
    query_source_article_id: int
    query_document_id: int | None
    embedding_version: str
    semantic_version: str
    status: SemanticAssessmentStatus
    reason: str | None
    lookback_hours: int
    requested_top_k: int
    query_chunks_available: int
    query_chunks_considered: int
    candidate_count: int
    best_native_distance: float | None
    evidence: tuple[dict[str, Any], ...]
    error_type: str | None
    safe_error: str | None
    evaluation_count: int
    created_at: datetime
    updated_at: datetime


class SemanticAssessmentRepository:
    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or connect_vector_db

    def save_assessment(self, draft: SemanticAssessmentDraft) -> int:
        evidence_json = json.dumps(
            draft.evidence,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            assessment_id = self._promote_provisional_assessment(
                cursor,
                draft,
                evidence_json,
            )
            if assessment_id is not None:
                connection.commit()
                return assessment_id

            cursor.execute(
                """
                INSERT INTO semantic_shadow_assessments
                    (query_source_article_id, query_document_id,
                     embedding_version, semantic_version, status, reason,
                     lookback_hours, requested_top_k,
                     query_chunks_available, query_chunks_considered,
                     candidate_count, best_native_distance, evidence_json,
                     error_type, safe_error)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    status = VALUES(status),
                    reason = VALUES(reason),
                    lookback_hours = VALUES(lookback_hours),
                    requested_top_k = VALUES(requested_top_k),
                    query_chunks_available = VALUES(query_chunks_available),
                    query_chunks_considered = VALUES(query_chunks_considered),
                    candidate_count = VALUES(candidate_count),
                    best_native_distance = VALUES(best_native_distance),
                    evidence_json = VALUES(evidence_json),
                    error_type = VALUES(error_type),
                    safe_error = VALUES(safe_error),
                    evaluation_count = evaluation_count + 1,
                    updated_at = UTC_TIMESTAMP()
                """,
                (
                    draft.query_source_article_id,
                    draft.query_document_id,
                    draft.embedding_version,
                    draft.semantic_version,
                    draft.status.value,
                    draft.reason,
                    draft.lookback_hours,
                    draft.requested_top_k,
                    draft.query_chunks_available,
                    draft.query_chunks_considered,
                    len(draft.evidence),
                    draft.best_native_distance,
                    evidence_json,
                    draft.error_type,
                    draft.safe_error,
                ),
            )
            assessment_id = int(cursor.lastrowid)
            connection.commit()
            return assessment_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _promote_provisional_assessment(
        cursor: Any,
        draft: SemanticAssessmentDraft,
        evidence_json: str,
    ) -> int | None:
        """Resolve a source-level provisional row to its durable document identity."""
        if draft.query_document_id is None:
            return None

        cursor.execute(
            """
            SELECT id, query_document_id, evaluation_count, created_at
            FROM semantic_shadow_assessments
            WHERE query_source_article_id=%s
              AND query_document_identity IN (0, %s)
              AND embedding_version=%s
              AND semantic_version=%s
            ORDER BY query_document_identity, id
            FOR UPDATE
            """,
            (
                draft.query_source_article_id,
                draft.query_document_id,
                draft.embedding_version,
                draft.semantic_version,
            ),
        )
        rows = cursor.fetchall()
        provisional = next(
            (row for row in rows if row["query_document_id"] is None),
            None,
        )
        if provisional is None:
            return None

        assessment_id = int(provisional["id"])
        evaluation_count = sum(int(row["evaluation_count"]) for row in rows) + 1
        created_at = min(row["created_at"] for row in rows)
        duplicate_ids = [
            int(row["id"])
            for row in rows
            if int(row["id"]) != assessment_id
        ]
        if duplicate_ids:
            placeholders = ", ".join("%s" for _ in duplicate_ids)
            cursor.execute(
                f"DELETE FROM semantic_shadow_assessments WHERE id IN ({placeholders})",
                tuple(duplicate_ids),
            )

        cursor.execute(
            """
            UPDATE semantic_shadow_assessments
            SET query_document_id=%s,
                status=%s,
                reason=%s,
                lookback_hours=%s,
                requested_top_k=%s,
                query_chunks_available=%s,
                query_chunks_considered=%s,
                candidate_count=%s,
                best_native_distance=%s,
                evidence_json=%s,
                error_type=%s,
                safe_error=%s,
                evaluation_count=%s,
                created_at=%s,
                updated_at=UTC_TIMESTAMP()
            WHERE id=%s
            """,
            (
                draft.query_document_id,
                draft.status.value,
                draft.reason,
                draft.lookback_hours,
                draft.requested_top_k,
                draft.query_chunks_available,
                draft.query_chunks_considered,
                len(draft.evidence),
                draft.best_native_distance,
                evidence_json,
                draft.error_type,
                draft.safe_error,
                evaluation_count,
                created_at,
                assessment_id,
            ),
        )
        return assessment_id

    def get_assessment(
        self,
        query_source_article_id: int,
        query_document_id: int | None,
        embedding_version: str,
        semantic_version: str,
    ) -> SemanticAssessmentRecord | None:
        if query_source_article_id <= 0:
            raise ValueError("query_source_article_id must be positive")
        document_identity = query_document_id or 0
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, query_source_article_id, query_document_id,
                       embedding_version, semantic_version, status, reason,
                       lookback_hours, requested_top_k,
                       query_chunks_available, query_chunks_considered,
                       candidate_count, best_native_distance, evidence_json,
                       error_type, safe_error, evaluation_count,
                       created_at, updated_at
                FROM semantic_shadow_assessments
                WHERE query_source_article_id=%s
                  AND query_document_identity=%s
                  AND embedding_version=%s
                  AND semantic_version=%s
                """,
                (
                    query_source_article_id,
                    document_identity,
                    embedding_version,
                    semantic_version,
                ),
            )
            row = cursor.fetchone()
            return self._record(row) if row else None
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _record(row: dict[str, Any]) -> SemanticAssessmentRecord:
        evidence = json.loads(row["evidence_json"])
        if not isinstance(evidence, list):
            raise ValueError("stored semantic evidence must be a JSON array")
        return SemanticAssessmentRecord(
            id=int(row["id"]),
            query_source_article_id=int(row["query_source_article_id"]),
            query_document_id=(
                int(row["query_document_id"])
                if row["query_document_id"] is not None
                else None
            ),
            embedding_version=row["embedding_version"],
            semantic_version=row["semantic_version"],
            status=SemanticAssessmentStatus(row["status"]),
            reason=row["reason"],
            lookback_hours=int(row["lookback_hours"]),
            requested_top_k=int(row["requested_top_k"]),
            query_chunks_available=int(row["query_chunks_available"]),
            query_chunks_considered=int(row["query_chunks_considered"]),
            candidate_count=int(row["candidate_count"]),
            best_native_distance=(
                float(row["best_native_distance"])
                if row["best_native_distance"] is not None
                else None
            ),
            evidence=tuple(evidence),
            error_type=row["error_type"],
            safe_error=row["safe_error"],
            evaluation_count=int(row["evaluation_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
