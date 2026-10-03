"""Atomic claim and state operations for durable publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import mysql.connector

from config import DB_CONFIG

from .state import is_claim_deadlock, new_claim_token


@dataclass(frozen=True)
class PublicationClaim:
    token: str
    article: dict[str, Any]
    attempt: int
    recovered: bool = False


class PublicationRepository:
    """Own the brief application-DB transactions around publication state."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or (lambda: mysql.connector.connect(**DB_CONFIG))

    def claim_next(
        self,
        *,
        timeout_minutes: int,
        fresh_start_after: str | None,
    ) -> PublicationClaim | None:
        for attempt_index in range(2):
            try:
                return self._claim_next_once(
                    timeout_minutes=timeout_minutes,
                    fresh_start_after=fresh_start_after,
                )
            except mysql.connector.Error as error:
                if not is_claim_deadlock(error):
                    raise
                if attempt_index == 1:
                    return None
        return None

    def _claim_next_once(
        self,
        *,
        timeout_minutes: int,
        fresh_start_after: str | None,
    ) -> PublicationClaim | None:
        token = new_claim_token()
        fresh_clause = ""
        params: list[Any] = [timeout_minutes]
        if fresh_start_after:
            fresh_clause = " AND c.insertDate >= %s"
            params.append(fresh_start_after)

        conn = self._connect()
        cursor = None
        try:
            conn.start_transaction()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"""
                SELECT r.*, c.id AS durable_raw_article_id,
                       c.scheduled_for, c.is_breaking
                FROM rich_crpytonews r
                JOIN cryptonewsapi c ON c.news_url = r.news_url
                WHERE r.published = 0
                  AND c.chosen_for_publish = 1
                  AND c.scheduled_for IS NOT NULL
                  AND c.scheduled_for <= UTC_TIMESTAMP()
                  AND (
                    r.publish_status IS NULL
                    OR r.publish_status IN ('pending', 'retryable')
                    OR (r.publish_status IN ('claimed', 'post_created')
                        AND r.publish_claimed_at < TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP()))
                  )
                  {fresh_clause}
                ORDER BY c.scheduled_for ASC, r.id ASC
                LIMIT 1
                FOR UPDATE
                """,
                tuple(params),
            )
            article = cursor.fetchone()
            if not article:
                conn.commit()
                return None

            cursor.execute(
                """
                UPDATE rich_crpytonews
                SET publish_status = 'claimed',
                    publish_claim_token = %s,
                    publish_claimed_at = UTC_TIMESTAMP(),
                    publish_attempt_count = publish_attempt_count + 1,
                    publish_last_error = NULL
                WHERE id = %s
                  AND published = 0
                  AND (
                    publish_status IS NULL
                    OR publish_status IN ('pending', 'retryable')
                    OR (publish_status IN ('claimed', 'post_created')
                        AND publish_claimed_at < TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP()))
                  )
                """,
                (token, article["id"], timeout_minutes),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            attempt = int(article.get("publish_attempt_count") or 0) + 1
            return PublicationClaim(
                token=token,
                article=article,
                attempt=attempt,
                recovered=article.get("publish_status") in ("claimed", "post_created"),
            )
        except BaseException:
            conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()

    def save_identity(
        self,
        rich_article_id: int,
        token: str,
        raw_article_id: int,
        publication_key: str,
    ) -> bool:
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET raw_article_id = COALESCE(raw_article_id, %s),
                publication_key = COALESCE(publication_key, %s)
            WHERE id = %s
              AND publish_claim_token = %s
              AND publish_status IN ('claimed', 'post_created')
            """,
            (raw_article_id, publication_key, rich_article_id, token),
        )

    def save_media(
        self,
        rich_article_id: int,
        token: str,
        media_id: int,
        metadata: Mapping[str, Any],
    ) -> bool:
        metadata_json = json.dumps(dict(metadata), ensure_ascii=True, sort_keys=True)
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET wp_media_id = %s, wp_media_metadata_json = %s
            WHERE id = %s
              AND publish_claim_token = %s
              AND publish_status IN ('claimed', 'post_created')
            """,
            (media_id, metadata_json, rich_article_id, token),
        )

    def clear_media(self, rich_article_id: int, token: str) -> bool:
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET wp_media_id = NULL, wp_media_metadata_json = NULL
            WHERE id = %s
              AND publish_claim_token = %s
              AND publish_status IN ('claimed', 'post_created')
            """,
            (rich_article_id, token),
        )

    def save_external_post(
        self,
        rich_article_id: int,
        token: str,
        post_id: int,
        post_url: str | None,
    ) -> bool:
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET wp_post_id = %s,
                wp_post_url = COALESCE(%s, wp_post_url),
                wp_post_created_at = COALESCE(wp_post_created_at, UTC_TIMESTAMP()),
                publish_status = 'post_created'
            WHERE id = %s
              AND publish_claim_token = %s
              AND publish_status IN ('claimed', 'post_created')
            """,
            (post_id, post_url, rich_article_id, token),
        )

    def complete(self, rich_article_id: int, token: str) -> bool:
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET published = 1,
                publish_status = 'published',
                published_at = COALESCE(published_at, UTC_TIMESTAMP()),
                publish_claim_token = NULL,
                publish_claimed_at = NULL,
                publish_last_error = NULL
            WHERE id = %s
              AND publish_claim_token = %s
              AND publish_status IN ('claimed', 'post_created')
            """,
            (rich_article_id, token),
        )

    def fail(self, rich_article_id: int, token: str, safe_error: str) -> bool:
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET publish_status = 'retryable',
                publish_claim_token = NULL,
                publish_claimed_at = NULL,
                publish_last_error = %s
            WHERE id = %s
              AND publish_claim_token = %s
              AND publish_status IN ('claimed', 'post_created')
            """,
            (safe_error[:500], rich_article_id, token),
        )

    def release_interrupted(self, rich_article_id: int, token: str) -> bool:
        return self.fail(rich_article_id, token, "Interrupted: publication will retry")

    def _owned_update(self, sql: str, params: tuple[Any, ...]) -> bool:
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            changed = cursor.rowcount == 1
            conn.commit()
            return changed
        except BaseException:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
