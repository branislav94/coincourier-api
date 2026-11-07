"""Atomic claim and state operations for durable publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Collection, Mapping

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
        exclude_rich_article_ids: Collection[int] = (),
        capacity_guard: bool = False,
        existing_posts_only: bool = False,
        reserved_posts_allowed: bool = False,
    ) -> PublicationClaim | None:
        """Claim the next eligible row, retaining caller exclusions across a deadlock retry."""
        excluded_ids = tuple(sorted(set(exclude_rich_article_ids)))
        for attempt_index in range(2):
            try:
                return self._claim_next_once(
                    timeout_minutes=timeout_minutes,
                    fresh_start_after=fresh_start_after,
                    exclude_rich_article_ids=excluded_ids,
                    capacity_guard=capacity_guard,
                    existing_posts_only=existing_posts_only,
                    reserved_posts_allowed=reserved_posts_allowed,
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
        exclude_rich_article_ids: Collection[int] = (),
        capacity_guard: bool = False,
        existing_posts_only: bool = False,
        reserved_posts_allowed: bool = False,
    ) -> PublicationClaim | None:
        token = new_claim_token()
        fresh_clause = ""
        exclude_clause = ""
        source_columns = ', c.publish_date AS source_publish_date, c.selected_at' if capacity_guard else ''
        identity_clause = ' AND r.wp_post_id IS NOT NULL' if existing_posts_only else ''
        if existing_posts_only and reserved_posts_allowed:
            identity_clause = ''' AND (r.wp_post_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM publication_capacity_reservations pcr
                WHERE pcr.rich_article_id = r.id AND pcr.utc_date = UTC_DATE()
                  AND pcr.create_started_at IS NULL))'''
        ordering = (
            '(r.wp_post_id IS NOT NULL) DESC, COALESCE(c.is_breaking, 0) DESC, '
            'c.scheduled_for ASC, r.id ASC'
            if capacity_guard else 'c.scheduled_for ASC, r.id ASC'
        )
        params: list[Any] = [timeout_minutes]
        if fresh_start_after:
            fresh_clause = " AND c.insertDate >= %s"
            params.append(fresh_start_after)
        if exclude_rich_article_ids:
            placeholders = ", ".join("%s" for _ in exclude_rich_article_ids)
            exclude_clause = f" AND r.id NOT IN ({placeholders})"
            params.extend(exclude_rich_article_ids)

        conn = self._connect()
        cursor = None
        try:
            conn.start_transaction()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"""
                SELECT r.*, c.id AS durable_raw_article_id,
                       c.scheduled_for, c.is_breaking {source_columns}
                FROM rich_crpytonews r
                JOIN cryptonewsapi c ON c.news_url = r.news_url
                WHERE r.published = 0
                  AND c.chosen_for_publish = 1
                  AND c.scheduled_for IS NOT NULL
                  AND c.scheduled_for <= UTC_TIMESTAMP()
                  AND (
                    r.publish_status IS NULL
                    OR r.publish_status IN ('fresh', 'quota_deferred', 'pending', 'retryable')
                    OR (r.publish_status IN ('claimed', 'post_created')
                        AND r.publish_claimed_at < TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP()))
                  )
                  {fresh_clause}
                  {exclude_clause}
                  {identity_clause}
                ORDER BY {ordering}
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
                    OR publish_status IN ('fresh', 'quota_deferred', 'pending', 'retryable')
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

    def begin_legacy_creation(self, rich_article_id: int, *, allow_quota_deferred: bool = False) -> bool:
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
            fresh_clause = (
                "publish_status IN ('fresh', 'quota_deferred')" if allow_quota_deferred
                else "publish_status = 'fresh'"
            )
            cursor.execute(
                f"""
                UPDATE rich_crpytonews
                SET publish_status = 'claimed',
                    publish_attempt_count = 1,
                    publish_claimed_at = UTC_TIMESTAMP()
                WHERE id = %s
                  AND published = 0
                  AND {fresh_clause}
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

    def reject_duplicate(
        self,
        rich_article_id: int,
        raw_article_id: int,
        decision: Any,
        *,
        token: str | None = None,
    ) -> bool:
        """Atomically consume an unpublished duplicate and its verified source.

        The caller has already persisted the policy audit. This transaction
        revalidates source identity, canonical external proof and ownership;
        a stale decision cannot modify a published or newly owned row.
        """
        canonical_raw_id = getattr(decision, "canonical_raw_id", None)
        canonical_rich_id = getattr(decision, "canonical_rich_id", None)
        canonical_wp_id = getattr(decision, "canonical_wp_id", None)
        ids = (rich_article_id, raw_article_id, canonical_raw_id, canonical_rich_id)
        if (
            any(type(value) is not int or value <= 0 for value in ids)
            or canonical_raw_id == raw_article_id
            or canonical_rich_id == rich_article_id
            or getattr(decision, "reject", False) is not True
            or getattr(decision, "available", False) is not True
            or getattr(decision, "mode", "") != "enforce"
            or getattr(decision, "stage", "") != "pre_publish"
            or getattr(decision, "article_id", None) != raw_article_id
            or (canonical_wp_id is not None and (type(canonical_wp_id) is not int or canonical_wp_id <= 0))
            or (token is not None and (not isinstance(token, str) or not token))
        ):
            raise ValueError("duplicate disposition requires a confirmed pre-publication decision")

        identity = """
            (r.raw_article_id = c.id OR (
                r.raw_article_id IS NULL
                AND c.news_url IS NOT NULL AND c.news_url <> ''
                AND r.news_url = c.news_url
                AND NOT EXISTS (
                    SELECT 1 FROM cryptonewsapi other_source
                    WHERE other_source.news_url = r.news_url AND other_source.id <> c.id
                )
            ))
        """
        ownership = (
            "r.publish_status = 'claimed' AND r.publish_claim_token = %s"
            if token is not None else
            "r.publish_status IN ('fresh', 'quota_deferred', 'pending', 'retryable') "
            "AND r.publish_claim_token IS NULL AND r.publish_claimed_at IS NULL"
        )
        ownership_params = (token,) if token is not None else ()
        conn = self._connect()
        cursor = None
        try:
            conn.start_transaction()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"""
                SELECT r.id, c.id AS raw_article_id,
                       c.processing_status, c.processing_claim_token,
                       c.processing_claimed_at, c.processing_last_error
                FROM rich_crpytonews r
                JOIN cryptonewsapi c ON c.id = %s
                WHERE r.id = %s AND {identity}
                  AND r.published = 0
                  AND r.wp_post_id IS NULL AND r.wp_post_url IS NULL
                  AND r.wp_post_created_at IS NULL
                  AND {ownership}
                  AND NOT EXISTS (
                    SELECT 1 FROM rich_crpytonews prior_post
                    WHERE (prior_post.raw_article_id = c.id
                        OR (prior_post.raw_article_id IS NULL AND prior_post.news_url = c.news_url))
                      AND (prior_post.published = 1 OR prior_post.wp_post_id > 0
                        OR prior_post.publish_status IN ('published', 'post_created'))
                  )
                LIMIT 1 FOR UPDATE
                """,
                (raw_article_id, rich_article_id, *ownership_params),
            )
            source_before = cursor.fetchone()
            if source_before is None:
                conn.rollback()
                return False
            if source_before.get("processing_status") == "claimed":
                expected_raw_state = {
                    name: source_before.get(name) for name in (
                        "processing_status", "processing_claim_token", "processing_claimed_at", "processing_last_error",
                    )
                }
            else:
                expected_raw_state = {
                    "processing_status": "duplicate" if source_before.get("processing_status") == "duplicate" else "completed",
                    "processing_claim_token": None,
                    "processing_claimed_at": None,
                    "processing_last_error": None,
                }

            post_expectation = " AND r.wp_post_id = %s" if canonical_wp_id is not None else ""
            cursor.execute(
                f"""
                SELECT r.id
                FROM rich_crpytonews r
                JOIN cryptonewsapi c ON c.id = %s
                WHERE r.id = %s AND {identity}
                  AND COALESCE(c.processing_status, '') <> 'duplicate'
                  AND COALESCE(r.publish_status, '') <> 'duplicate'
                  AND (r.wp_post_id > 0 OR r.published = 1
                       OR r.publish_status IN ('published', 'post_created'))
                  {post_expectation}
                LIMIT 1 FOR UPDATE
                """,
                (canonical_raw_id, canonical_rich_id, *((canonical_wp_id,) if canonical_wp_id is not None else ())),
            )
            if cursor.fetchone() is None:
                conn.rollback()
                return False

            update_ownership = ownership.replace("r.", "")
            cursor.execute(
                f"""
                UPDATE rich_crpytonews
                SET publish_status = 'duplicate',
                    publish_claim_token = NULL,
                    publish_claimed_at = NULL,
                    publish_last_error = NULL
                WHERE id = %s AND published = 0
                  AND wp_post_id IS NULL AND wp_post_url IS NULL
                  AND wp_post_created_at IS NULL
                  AND {update_ownership}
                """,
                (rich_article_id, *ownership_params),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            # MySQL/MariaDB evaluate SET left to right: inspect the original
            # status for each claim field, then assign status last. The raw
            # worker may have stored rich content just before completing.
            cursor.execute(
                """
                UPDATE cryptonewsapi
                SET processed = 1,
                    chosen_for_publish = 0,
                    scheduled_for = NULL,
                    processing_claim_token = CASE WHEN processing_status = 'claimed'
                                                 THEN processing_claim_token ELSE NULL END,
                    processing_claimed_at = CASE WHEN processing_status = 'claimed'
                                                THEN processing_claimed_at ELSE NULL END,
                    processing_last_error = CASE WHEN processing_status = 'claimed'
                                                THEN processing_last_error ELSE NULL END,
                    processing_status = CASE WHEN processing_status IN ('claimed', 'duplicate')
                                             THEN processing_status ELSE 'completed' END
                WHERE id = %s
                """,
                (raw_article_id,),
            )
            cursor.execute(
                """
                SELECT processed, chosen_for_publish, scheduled_for, processing_status,
                       processing_claim_token, processing_claimed_at, processing_last_error
                FROM cryptonewsapi WHERE id = %s
                """,
                (raw_article_id,),
            )
            source_state = cursor.fetchone()
            if not (
                source_state is not None
                and source_state.get("processed") == 1
                and source_state.get("chosen_for_publish") == 0
                and source_state.get("scheduled_for") is None
                and all(source_state.get(name) == value for name, value in expected_raw_state.items())
            ):
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

    def defer_duplicate_guard(
        self,
        rich_article_id: int,
        token: str,
        *,
        creation_is_fresh: bool,
    ) -> bool:
        """Release a first claim only when no external creation was attempted."""
        if type(rich_article_id) is not int or rich_article_id <= 0 or not isinstance(token, str) or not token:
            raise ValueError("duplicate guard deferral requires an owned article")
        if creation_is_fresh is not True:
            return self.require_manual_reconciliation(
                rich_article_id, token, "duplicate_guard_creation_not_fresh",
            )
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET publish_status = 'fresh',
                publish_attempt_count = 0,
                publish_claim_token = NULL,
                publish_claimed_at = NULL,
                publish_last_error = NULL
            WHERE id = %s
              AND publish_claim_token = %s
              AND published = 0
              AND publish_status = 'claimed'
              AND publish_attempt_count = 1
              AND wp_post_id IS NULL
              AND wp_post_url IS NULL
              AND wp_post_created_at IS NULL
            """,
            (rich_article_id, token),
        )

    def defer_capacity(self, rich_article_id: int, token: str, code: str) -> bool:
        """Restore first-attempt provenance only after a pre-CREATE quota deferral.

        The capacity ledger independently fences any committed creation intent.
        Never reset a prior attempt, linked post, or another worker's claim.
        """
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET publish_status = 'quota_deferred', publish_attempt_count = 0,
                publish_claim_token = NULL, publish_claimed_at = NULL,
                publish_last_error = %s
            WHERE id = %s AND publish_claim_token = %s
              AND published = 0 AND publish_status = 'claimed'
              AND publish_attempt_count = 1
              AND wp_post_id IS NULL AND wp_post_url IS NULL
              AND wp_post_created_at IS NULL
            """,
            (code[:500], rich_article_id, token),
        )

    def defer_legacy_capacity(self, rich_article_id: int, code: str) -> bool:
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET publish_status = 'quota_deferred', publish_last_error = %s
            WHERE id = %s AND published = 0
              AND publish_status IN ('fresh', 'quota_deferred')
              AND publish_attempt_count = 0 AND publish_claim_token IS NULL
              AND wp_post_id IS NULL AND wp_post_url IS NULL
              AND wp_post_created_at IS NULL
            """,
            (code[:500], rich_article_id),
        )

    def defer_legacy_freshness(self, rich_article_id: int) -> bool:
        return self._owned_update(
            """
            UPDATE rich_crpytonews
            SET publish_status = 'manual_review',
                publish_last_error = 'stale_backlog_review_required'
            WHERE id = %s AND published = 0 AND wp_post_id IS NULL
              AND publish_claim_token IS NULL
              AND publish_status IN ('fresh', 'quota_deferred')
              AND publish_attempt_count = 0
            """,
            (rich_article_id,),
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
