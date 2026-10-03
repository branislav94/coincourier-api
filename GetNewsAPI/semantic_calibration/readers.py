"""Read-only evidence adapters for application and vector databases."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Protocol

from duplicate_detection.policy import AssessmentType
from vector_store.semantic_assessments import SemanticAssessmentStatus

from .models import (
    DeterministicEvidence,
    SemanticCandidateEvidence,
    SemanticEvidenceSnapshot,
)


Pair = tuple[int, int]


class DeterministicEvidenceReader(Protocol):
    def load_evidence(
        self,
        pairs: Iterable[Pair],
        *,
        policy_version: str,
    ) -> Mapping[Pair, DeterministicEvidence]:
        ...


class SemanticEvidenceReader(Protocol):
    def load_evidence(
        self,
        query_source_article_ids: Iterable[int],
        *,
        embedding_version: str,
        semantic_version: str,
    ) -> Mapping[int, SemanticEvidenceSnapshot]:
        ...


class InMemoryDeterministicEvidenceReader:
    def __init__(self, evidence: Iterable[DeterministicEvidence]) -> None:
        self._evidence = tuple(evidence)

    def load_evidence(
        self,
        pairs: Iterable[Pair],
        *,
        policy_version: str,
    ) -> Mapping[Pair, DeterministicEvidence]:
        requested = set(pairs)
        return {
            (item.query_source_article_id, item.candidate_source_article_id): item
            for item in self._evidence
            if item.policy_version == policy_version
            and (item.query_source_article_id, item.candidate_source_article_id)
            in requested
        }


class InMemorySemanticEvidenceReader:
    def __init__(self, evidence: Iterable[SemanticEvidenceSnapshot]) -> None:
        self._evidence = tuple(evidence)

    def load_evidence(
        self,
        query_source_article_ids: Iterable[int],
        *,
        embedding_version: str,
        semantic_version: str,
    ) -> Mapping[int, SemanticEvidenceSnapshot]:
        requested = set(query_source_article_ids)
        return {
            item.query_source_article_id: item
            for item in self._evidence
            if item.query_source_article_id in requested
            and item.embedding_version == embedding_version
            and item.semantic_version == semantic_version
        }


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _string_tuple(value: Any) -> tuple[str, ...]:
    parsed = _json_value(value)
    if not isinstance(parsed, list):
        raise ValueError("stored deterministic evidence must be a JSON array")
    return tuple(str(item) for item in parsed)


def _reason_codes(value: Any) -> tuple[str, ...]:
    parsed = _json_value(value)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("codes"), list):
        raise ValueError("stored deterministic reason evidence is invalid")
    return tuple(str(item) for item in parsed["codes"])


class MariaDBDeterministicEvidenceReader:
    """Read selected Phase 5 assessments without opening a write transaction."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        if connect is None:
            import mysql.connector

            from config import DB_CONFIG

            connect = lambda: mysql.connector.connect(**DB_CONFIG)
        self._connect = connect

    def load_evidence(
        self,
        pairs: Iterable[Pair],
        *,
        policy_version: str,
    ) -> Mapping[Pair, DeterministicEvidence]:
        requested = set(pairs)
        if not requested:
            return {}
        query_ids = sorted({query_id for query_id, _ in requested})
        placeholders = ", ".join("%s" for _ in query_ids)
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT article_id, candidate_article_id, assessment_type,
                       same_provider_article_id, same_event_id,
                       same_canonical_url, same_content_hash,
                       title_token_jaccard, publication_distance_hours,
                       shared_entities_json, shared_dates_json,
                       shared_numbers_json, reason_json, policy_version
                FROM duplicate_assessments
                WHERE policy_version=%s
                  AND article_id IN ({placeholders})
                ORDER BY article_id, candidate_article_id, id DESC
                """,
                (policy_version, *query_ids),
            )
            evidence: dict[Pair, DeterministicEvidence] = {}
            for row in cursor.fetchall():
                pair = (int(row["article_id"]), int(row["candidate_article_id"]))
                if pair not in requested or pair in evidence:
                    continue
                evidence[pair] = DeterministicEvidence(
                    query_source_article_id=pair[0],
                    candidate_source_article_id=pair[1],
                    classification=AssessmentType(row["assessment_type"]),
                    same_provider_article_id=bool(row["same_provider_article_id"]),
                    same_event_id=bool(row["same_event_id"]),
                    same_canonical_url=bool(row["same_canonical_url"]),
                    same_content_hash=bool(row["same_content_hash"]),
                    title_token_jaccard=float(row["title_token_jaccard"]),
                    publication_distance_hours=(
                        float(row["publication_distance_hours"])
                        if row["publication_distance_hours"] is not None
                        else None
                    ),
                    shared_entities=_string_tuple(row["shared_entities_json"]),
                    shared_dates=_string_tuple(row["shared_dates_json"]),
                    shared_numbers=_string_tuple(row["shared_numbers_json"]),
                    reason_codes=_reason_codes(row["reason_json"]),
                    policy_version=row["policy_version"],
                )
            return evidence
        finally:
            cursor.close()
            connection.close()


class MariaDBSemanticEvidenceReader:
    """Read the latest selected-version assessment for each source article."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        if connect is None:
            from vector_store.db import connect_vector_db

            connect = connect_vector_db
        self._connect = connect

    def load_evidence(
        self,
        query_source_article_ids: Iterable[int],
        *,
        embedding_version: str,
        semantic_version: str,
    ) -> Mapping[int, SemanticEvidenceSnapshot]:
        query_ids = sorted(set(query_source_article_ids))
        if not query_ids:
            return {}
        placeholders = ", ".join("%s" for _ in query_ids)
        connection = self._connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT id, query_source_article_id, query_document_id, status,
                       embedding_version, semantic_version, evidence_json,
                       evaluation_count, updated_at
                FROM semantic_shadow_assessments
                WHERE embedding_version=%s
                  AND semantic_version=%s
                  AND query_source_article_id IN ({placeholders})
                ORDER BY query_source_article_id, updated_at DESC, id DESC
                """,
                (embedding_version, semantic_version, *query_ids),
            )
            evidence: dict[int, SemanticEvidenceSnapshot] = {}
            for row in cursor.fetchall():
                query_id = int(row["query_source_article_id"])
                if query_id in evidence:
                    continue
                status = SemanticAssessmentStatus(row["status"])
                raw_candidates = _json_value(row["evidence_json"])
                if not isinstance(raw_candidates, list):
                    raise ValueError("stored semantic evidence must be a JSON array")
                candidates = tuple(
                    SemanticCandidateEvidence(
                        candidate_source_article_id=int(
                            item["candidate_source_article_id"]
                        ),
                        candidate_document_id=int(item["candidate_document_id"]),
                        rank=rank,
                        native_distance=float(item["native_distance"]),
                        best_query_chunk_index=int(item["best_query_chunk_index"]),
                        best_candidate_chunk_index=int(
                            item["best_candidate_chunk_index"]
                        ),
                        matched_query_chunk_count=int(
                            item["matched_query_chunk_count"]
                        ),
                        publication_delta_hours=float(
                            item["publication_delta_hours"]
                        ),
                    )
                    for rank, item in enumerate(raw_candidates, start=1)
                )
                evidence[query_id] = SemanticEvidenceSnapshot(
                    assessment_id=int(row["id"]),
                    query_source_article_id=query_id,
                    query_document_id=(
                        int(row["query_document_id"])
                        if row["query_document_id"] is not None
                        else None
                    ),
                    status=status,
                    embedding_version=row["embedding_version"],
                    semantic_version=row["semantic_version"],
                    candidates=candidates,
                    evaluation_count=int(row["evaluation_count"]),
                )
            return evidence
        finally:
            cursor.close()
            connection.close()
