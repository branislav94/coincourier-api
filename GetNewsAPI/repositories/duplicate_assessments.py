"""Bounded reads and idempotent writes for duplicate shadow assessments."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

import mysql.connector

from config import DB_CONFIG
from duplicate_detection.policy import DuplicateAssessment


class DuplicateAssessmentRepository:
    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or (lambda: mysql.connector.connect(**DB_CONFIG))

    def load_candidates(
        self,
        article_id: int,
        *,
        lookback_hours: int,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, news_id, event_id, news_url, canonical_url,
                       title, title_hash, full_text, publish_date,
                       source_name, topics, tickers, processed,
                       chosen_for_publish
                FROM cryptonewsapi
                WHERE id <> %s
                  AND publish_date >= TIMESTAMPADD(HOUR, -%s, UTC_TIMESTAMP())
                  AND (chosen_for_publish = 1 OR processed = 1)
                ORDER BY publish_date DESC, id DESC
                LIMIT %s
                """,
                (article_id, lookback_hours, limit),
            )
            return list(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    def save_assessments(
        self,
        article_id: int,
        assessments: Iterable[tuple[int, DuplicateAssessment]],
    ) -> int:
        rows = [
            (
                article_id,
                candidate_id,
                assessment.assessment_type.value,
                int(assessment.same_provider_article_id),
                int(assessment.same_event_id),
                int(assessment.same_canonical_url),
                int(assessment.same_content_hash),
                assessment.title_token_jaccard,
                assessment.publication_distance_hours,
                json.dumps(assessment.shared_entities, separators=(",", ":")),
                json.dumps(assessment.shared_dates, separators=(",", ":")),
                json.dumps(assessment.shared_numbers, separators=(",", ":")),
                json.dumps(
                    {"codes": assessment.reason_codes},
                    separators=(",", ":"),
                ),
                assessment.policy_version,
            )
            for candidate_id, assessment in assessments
            if candidate_id != article_id
        ]
        if not rows:
            return 0

        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.executemany(
                """
                INSERT INTO duplicate_assessments
                    (article_id, candidate_article_id, assessment_type,
                     same_provider_article_id, same_event_id,
                     same_canonical_url, same_content_hash,
                     title_token_jaccard, publication_distance_hours,
                     shared_entities_json, shared_dates_json,
                     shared_numbers_json, reason_json, policy_version)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    assessment_type = VALUES(assessment_type),
                    same_provider_article_id = VALUES(same_provider_article_id),
                    same_event_id = VALUES(same_event_id),
                    same_canonical_url = VALUES(same_canonical_url),
                    same_content_hash = VALUES(same_content_hash),
                    title_token_jaccard = VALUES(title_token_jaccard),
                    publication_distance_hours = VALUES(publication_distance_hours),
                    shared_entities_json = VALUES(shared_entities_json),
                    shared_dates_json = VALUES(shared_dates_json),
                    shared_numbers_json = VALUES(shared_numbers_json),
                    reason_json = VALUES(reason_json),
                    updated_at = UTC_TIMESTAMP()
                """,
                rows,
            )
            conn.commit()
            return len(rows)
        except BaseException:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
