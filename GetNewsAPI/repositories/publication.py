"""Atomic claim and state operations for durable publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import mysql.connector

from config import DB_CONFIG

from .state import is_claim_deadlock, new_claim_token


_UNSPECIFIED_POST_STATE = object()


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
                    OR r.publish_status IN ('fresh', 'pending', 'retryable')
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
                    OR publish_status IN ('fresh', 'pending', 'retryable')
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
        *,
        expected_post_id: Any = _UNSPECIFIED_POST_STATE,
        expected_post_url: Any = _UNSPECIFIED_POST_STATE,
    ) -> bool:
        """Fill or verify an owned identity without relying on changed-row count.

        Explicit post-pointer expectations protect the claim snapshot, including
        an expected NULL. Omitted expectations preserve the older call signature.
        A no-op UPDATE succeeds only after this transaction verifies ownership
        and the exact identity; conflicting values are never overwritten.
        """
        post_predicates = ""
        params: list[Any] = [
            raw_article_id, publication_key, rich_article_id, token,
            raw_article_id, publication_key,
        ]
        if expected_post_id is not _UNSPECIFIED_POST_STATE:
            post_predicates += " AND wp_post_id <=> %s"
            params.append(expected_post_id)
        if expected_post_url is not _UNSPECIFIED_POST_STATE:
            post_predicates += " AND wp_post_url <=> %s"
            params.append(expected_post_url)

        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"""
                UPDATE rich_crpytonews
                SET raw_article_id = COALESCE(raw_article_id, %s),
                    publication_key = COALESCE(publication_key, %s)
                WHERE id = %s
                  AND publish_claim_token = %s
                  AND published = 0
                  AND publish_status IN ('claimed', 'post_created')
                  AND (raw_article_id IS NULL OR raw_article_id = %s)
                  AND (publication_key IS NULL OR publication_key = %s)
                  {post_predicates}
                """,
                tuple(params),
            )
            changed = cursor.rowcount
            cursor.execute(
                """
                SELECT id, published, publish_status, publish_claim_token,
                       raw_article_id, publication_key, wp_post_id, wp_post_url
                FROM rich_crpytonews
                WHERE id = %s
                  AND publish_claim_token = %s
                  AND published = 0
                  AND publish_status IN ('claimed', 'post_created')
                LIMIT 1
                FOR UPDATE
                """,
                (rich_article_id, token),
            )
            state = cursor.fetchone()
            verified = (
                changed in (0, 1)
                and state is not None
                and state.get("id") == rich_article_id
                and state.get("published") == 0
                and state.get("publish_claim_token") == token
                and state.get("publish_status") in ("claimed", "post_created")
                and state.get("raw_article_id") == raw_article_id
                and state.get("publication_key") == publication_key
                and (
                    expected_post_id is _UNSPECIFIED_POST_STATE
                    or state.get("wp_post_id") == expected_post_id
                )
                and (
                    expected_post_url is _UNSPECIFIED_POST_STATE
                    or state.get("wp_post_url") == expected_post_url
                )
            )
            if not verified:
                conn.rollback()
                return False
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()

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

    def get_external_post_state(self, rich_article_id: int) -> dict[str, Any]:
        '''Read publication identity and attempt state before legacy creation.

        Missing rows or required state columns are fatal: publishing must prove
        fresh permission and durably retain any returned publication identity.
        '''
        if (
            isinstance(rich_article_id, bool)
            or not isinstance(rich_article_id, int)
            or rich_article_id <= 0
        ):
            raise ValueError('rich article ID must be a positive integer')
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                '''
                SELECT id, wp_post_id, wp_post_url, published, publish_status,
                       publish_attempt_count, publish_claim_token, wp_post_created_at
                FROM rich_crpytonews
                WHERE id = %s AND published = 0
                LIMIT 1
                ''',
                (rich_article_id,),
            )
            state = cursor.fetchone()
            if state is None:
                raise RuntimeError('unpublished application post identity is unavailable')
            return dict(state)
        except BaseException:
            conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()

    def begin_legacy_creation(self, rich_article_id: int) -> bool:
        """Commit creation intent only for an explicitly fresh legacy article.

        The publisher holds the existing application advisory lock. This durable
        transition occurs before the remote POST so an uncertain creation cannot
        regain fresh permission on a later run without a saved post pointer.
        """
        if (
            isinstance(rich_article_id, bool)
            or not isinstance(rich_article_id, int)
            or rich_article_id <= 0
        ):
            raise ValueError("rich article ID must be a positive integer")
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE rich_crpytonews
                SET publish_status = 'claimed',
                    publish_attempt_count = 1,
                    publish_claimed_at = UTC_TIMESTAMP()
                WHERE id = %s
                  AND published = 0
                  AND publish_status = 'fresh'
                  AND publish_attempt_count = 0
                  AND wp_post_id IS NULL
                  AND wp_post_url IS NULL
                  AND wp_post_created_at IS NULL
                  AND publish_claim_token IS NULL
                """,
                (rich_article_id,),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()

    def save_legacy_external_post(
        self,
        rich_article_id: int,
        post_id: int,
        post_url: str | None,
    ) -> bool:
        '''Persist and verify a legacy post pointer without replacing another ID.

        The publisher already holds its application advisory lock. Readback in
        this transaction also handles an idempotent UPDATE affecting zero rows.
        Only the existing application post-ID and URL columns are required.
        '''
        for value in (rich_article_id, post_id):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError('article and WordPress post IDs must be positive integers')
        if post_url is not None and not isinstance(post_url, str):
            raise ValueError('WordPress post URL must be a string or None')
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                '''
                UPDATE rich_crpytonews
                SET wp_post_id = %s,
                    wp_post_url = COALESCE(%s, wp_post_url)
                WHERE id = %s
                  AND published = 0
                  AND (wp_post_id IS NULL OR wp_post_id = %s)
                ''',
                (post_id, post_url, rich_article_id, post_id),
            )
            cursor.execute(
                '''
                SELECT id, wp_post_id, wp_post_url
                FROM rich_crpytonews
                WHERE id = %s AND published = 0
                LIMIT 1
                ''',
                (rich_article_id,),
            )
            state = cursor.fetchone()
            verified = (
                state is not None
                and state.get('wp_post_id') == post_id
                and (post_url is None or state.get('wp_post_url') == post_url)
            )
            if not verified:
                conn.rollback()
                return False
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()

    def defer_legacy_manual_reconciliation(self, rich_article_id: int) -> bool:
        """Keep unresolved legacy rows out of later automatic publication batches.

        The existing application advisory lock protects this legacy transition.
        Preserve any captured URL, timestamps and media while requiring a
        missing post ID; an already linked post must remain eligible for repair.
        """
        if (
            isinstance(rich_article_id, bool)
            or not isinstance(rich_article_id, int)
            or rich_article_id <= 0
        ):
            raise ValueError("rich article ID must be a positive integer")
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET publish_status = 'manual_review',
                publish_claim_token = NULL,
                publish_claimed_at = NULL,
                publish_last_error = %s
            WHERE id = %s
              AND published = 0
              AND wp_post_id IS NULL
              AND publish_claim_token IS NULL
              AND (publish_status IS NULL OR publish_status <> 'manual_review')
            """,
            ("post_identity_manual_reconciliation_required", rich_article_id),
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

    def require_manual_reconciliation(
        self,
        rich_article_id: int,
        token: str,
        code: str,
    ) -> bool:
        """Release an owned unresolved attempt without making it retryable.

        The publisher supplies a controlled diagnostic code. Keeping this state
        outside claim eligibility lets later linked or fresh articles proceed.
        """
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET publish_status = 'manual_review',
                publish_claim_token = NULL,
                publish_claimed_at = NULL,
                publish_last_error = %s
            WHERE id = %s
              AND publish_claim_token = %s
              AND published = 0
              AND publish_status IN ('claimed', 'post_created')
              AND wp_post_id IS NULL
            """,
            (code[:500], rich_article_id, token),
        )

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
