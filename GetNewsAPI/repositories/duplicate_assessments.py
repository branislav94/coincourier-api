"""Bounded reads and idempotent writes for duplicate shadow assessments."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping

import mysql.connector

from config import DB_CONFIG
from duplicate_detection.policy import DuplicateAssessment


class DuplicateAssessmentRepository:
    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or (lambda: mysql.connector.connect(**DB_CONFIG))

    def load_source_article(self, raw_id: int) -> dict[str, Any] | None:
        """Read original provider evidence, never the rewritten rich body."""
        conn = self._connect()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("SELECT * FROM cryptonewsapi WHERE id = %s", (raw_id,))
            return cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

    def load_enforcement_candidates(
        self,
        article_id: int,
        *,
        lookback_hours: int,
        stage: str,
        source_article_ids: Iterable[int] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Only established primaries can suppress another source article.

        Selected unprocessed rows and terminal duplicate rows are never anchors.
        The pre-publish check additionally requires an external post outcome.
        """
        source_ids = tuple(sorted({int(item) for item in source_article_ids if int(item) > 0}))[:200]
        semantic_clause = ""
        params: list[Any] = [article_id, max(1, int(lookback_hours))]
        if source_ids:
            semantic_clause = " OR c.id IN (" + ", ".join("%s" for _ in source_ids) + ")"
            params.extend(source_ids)
        external_clause = ""
        credibility_clause = " AND (c.processed = 1 OR r.wp_post_id > 0 OR r.published = 1)"
        terminal_clause = " AND COALESCE(r.publish_status, '') NOT IN ('duplicate', 'manual_review')"
        if stage == "pre_publish":
            external_clause = """ AND (r.wp_post_id > 0 OR r.published = 1 OR r.publish_status IN ('published', 'post_created')
                OR (r.publish_attempt_count > 0 AND r.publish_status IN ('claimed', 'retryable', 'manual_review')))"""
            terminal_clause = " AND COALESCE(r.publish_status, '') <> 'duplicate'"
            credibility_clause = ""
        params.append(min(200, max(1, int(limit))))
        conn = self._connect()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT c.*, r.id AS canonical_rich_id,
                       r.wp_post_id AS canonical_wp_id,
                       r.wp_post_url AS canonical_wp_url,
                       r.published AS canonical_published,
                       r.publish_status AS canonical_publish_status,
                       r.publish_attempt_count AS canonical_publish_attempt_count,
                       r.published_at AS canonical_published_at,
                       r.wp_post_created_at AS canonical_post_created_at
                FROM cryptonewsapi c
                JOIN rich_crpytonews r
                  ON (r.raw_article_id = c.id
                      OR (r.raw_article_id IS NULL AND r.news_url = c.news_url))
                WHERE c.id <> %s
                  AND (c.publish_date >= TIMESTAMPADD(HOUR, -%s, UTC_TIMESTAMP()){semantic_clause})
                  {credibility_clause}
                  AND COALESCE(c.processing_status, '') <> 'duplicate'
                  {terminal_clause}
                  {external_clause}
                ORDER BY (r.wp_post_id > 0 OR r.published = 1 OR r.publish_status = 'post_created') DESC,
                         COALESCE(r.published_at, r.wp_post_created_at, c.publish_date) ASC,
                         c.id ASC, r.id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            return list(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

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
        *,
        audit_context: Mapping[str, Any] | None = None,
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
                    {
                        "codes": assessment.reason_codes,
                        **({"enforcement": {
                            **{key: value for key, value in audit_context.items() if key != "candidate_audits"},
                            **audit_context.get("candidate_audits", {}).get(candidate_id, {}),
                        }} if audit_context is not None else {}),
                    },
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
