"""Atomic claim and completion operations for raw-news processing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import mysql.connector

from config import DB_CONFIG

from .state import is_claim_deadlock, new_claim_token


@dataclass(frozen=True)
class ProcessingClaim:
    token: str
    article: dict[str, Any]
    attempt: int
    recovered: bool = False


class RawNewsRepository:
    """Own the brief transactions around processing claim state."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or (lambda: mysql.connector.connect(**DB_CONFIG))

    def claim_next(
        self,
        *,
        timeout_minutes: int,
        lookahead_minutes: int | None,
        fresh_start_after: str | None,
    ) -> ProcessingClaim | None:
        for attempt_index in range(2):
            try:
                return self._claim_next_once(
                    timeout_minutes=timeout_minutes,
                    lookahead_minutes=lookahead_minutes,
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
        lookahead_minutes: int | None,
        fresh_start_after: str | None,
    ) -> ProcessingClaim | None:
        token = new_claim_token()
        clauses = [
            "processed = 0",
            "chosen_for_publish = 1",
            "("
            "processing_status IS NULL "
            "OR processing_status IN ('pending', 'retryable') "
            "OR (processing_status = 'claimed' "
            "AND processing_claimed_at < TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP()))"
            ")",
        ]
        params: list[Any] = [timeout_minutes]
        if fresh_start_after:
            clauses.append("insertDate >= %s")
            params.append(fresh_start_after)
        if lookahead_minutes is not None:
            clauses.extend(
                [
                    "scheduled_for IS NOT NULL",
                    "scheduled_for <= TIMESTAMPADD(MINUTE, %s, UTC_TIMESTAMP())",
                ]
            )
            params.append(lookahead_minutes)

        conn = self._connect()
        cursor = None
        try:
            conn.start_transaction()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"""
                SELECT *
                FROM cryptonewsapi
                WHERE {' AND '.join(clauses)}
                ORDER BY scheduled_for ASC, selected_at ASC, id ASC
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
                UPDATE cryptonewsapi
                SET processing_status = 'claimed',
                    processing_claim_token = %s,
                    processing_claimed_at = UTC_TIMESTAMP(),
                    processing_attempt_count = processing_attempt_count + 1,
                    processing_last_error = NULL
                WHERE id = %s
                  AND processed = 0
                  AND (
                    processing_status IS NULL
                    OR processing_status IN ('pending', 'retryable')
                    OR (processing_status = 'claimed'
                        AND processing_claimed_at < TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP()))
                  )
                """,
                (token, article["id"], timeout_minutes),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            attempt = int(article.get("processing_attempt_count") or 0) + 1
            return ProcessingClaim(
                token=token,
                article=article,
                attempt=attempt,
                recovered=article.get("processing_status") == "claimed",
            )
        except BaseException:
            conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()

    def complete(self, raw_article_id: int, token: str) -> bool:
        return self._finish(
            raw_article_id,
            token,
            """
            UPDATE cryptonewsapi
            SET processed = 1,
                processing_status = 'completed',
                processing_claim_token = NULL,
                processing_claimed_at = NULL,
                processing_last_error = NULL
            WHERE id = %s
              AND processing_status = 'claimed'
              AND processing_claim_token = %s
            """,
            (raw_article_id, token),
        )

    def fail(self, raw_article_id: int, token: str, safe_error: str) -> bool:
        return self._finish(
            raw_article_id,
            token,
            """
            UPDATE cryptonewsapi
            SET processing_status = 'retryable',
                processing_claim_token = NULL,
                processing_claimed_at = NULL,
                processing_last_error = %s
            WHERE id = %s
              AND processing_status = 'claimed'
              AND processing_claim_token = %s
            """,
            (safe_error[:500], raw_article_id, token),
        )

    def release_interrupted(self, raw_article_id: int, token: str) -> bool:
        return self.fail(raw_article_id, token, "Interrupted: processing will retry")

    def _finish(
        self,
        raw_article_id: int,
        token: str,
        sql: str,
        params: tuple[Any, ...],
    ) -> bool:
        del raw_article_id, token
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
