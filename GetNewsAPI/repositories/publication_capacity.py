"""Short, database-only transactions for the optional UTC publication ceiling.

A reservation is never expired automatically. Once ``begin_create`` commits,
the slot remains consumed even if the caller cannot establish the HTTP outcome.
The caller must use the returned timestamp as WordPress ``date_gmt`` and must
never retry a CREATE after ``CapacityAlreadyAttempted``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, TypeVar

import mysql.connector

from .state import is_claim_deadlock


class CapacityUnavailable(RuntimeError):
    """The capacity store cannot safely authorize a new publication."""


class CapacityAlreadyAttempted(CapacityUnavailable):
    """This identity already crossed the irreversible CREATE boundary."""


@dataclass(frozen=True)
class CapacitySnapshot:
    utc_date: date
    remaining: int
    limit: int


@dataclass(frozen=True)
class CapacityReservation:
    rich_article_id: int
    publication_key: str
    utc_date: date
    publication_at: datetime | None = None
    create_started_at: datetime | None = None
    wp_post_id: int | None = None


class _DayChanged(Exception):
    """Retry a transaction that waited for a day guard across UTC midnight."""


_T = TypeVar("_T")


def _default_connect():
    # Injected test connections do not load application credentials or .env.
    from config import DB_CONFIG

    return mysql.connector.connect(**DB_CONFIG)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CapacityRepository:
    """Serialize admissions using the current day's InnoDB guard row.

    Lock order is always day guard, then reservation rows. ``record_created``
    only locks a reservation and never subsequently acquires a day guard.
    Transactions contain no external requests and retry only whole deadlocked
    transactions (or a midnight rollover before the CREATE marker is written).
    """

    def __init__(
        self,
        limit: int,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4294967295:
            raise ValueError("publication daily hard cap must be between 1 and 4294967295")
        self.limit = limit
        self._connect = connect or _default_connect
        self._schema_checked = False

    def snapshot(self) -> CapacitySnapshot:
        """Read an advisory snapshot; absence of required schema fails closed.

        This read does not initialize a day, claim a slot, or promise that the
        observed capacity will still be available during a subsequent reserve.
        """
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor(dictionary=True)
            self._ensure_schema(cursor)
            cursor.execute(
                """
                SELECT UTC_DATE() AS `utc_date`, d.`utc_date` AS guard_date,
                       d.hard_cap, d.baseline_count,
                       (SELECT COUNT(*)
                        FROM publication_capacity_reservations
                        WHERE `utc_date` = UTC_DATE()) AS reserved_count
                FROM (SELECT 1) AS clock
                LEFT JOIN publication_capacity_days d ON d.`utc_date` = UTC_DATE()
                """
            )
            row = cursor.fetchone()
            if row is None:
                raise CapacityUnavailable("publication capacity snapshot is unavailable")
            self._validate_guard(row, allow_missing=row.get("guard_date") is None)
            used = int(row.get("baseline_count") or 0) + int(row["reserved_count"])
            if used > self.limit:
                raise CapacityUnavailable("publication capacity is overcommitted")
            return CapacitySnapshot(row["utc_date"], max(0, self.limit - used), self.limit)
        except CapacityUnavailable:
            raise
        except Exception as error:
            raise CapacityUnavailable("publication capacity snapshot failed") from error
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def reserve(self, rich_article_id: int, publication_key: str) -> CapacityReservation | None:
        """Reserve before expensive image work, reusing an unattempted identity.

        An unattempted previous-day reservation moves only when today's guard
        has room. Otherwise it remains intact and the caller must defer.
        """
        self._validate_identity(rich_article_id, publication_key)
        return self._transaction(
            lambda cursor: self._reserve(cursor, rich_article_id, publication_key, begin=False)
        )

    def begin_create(self, rich_article_id: int, publication_key: str) -> datetime | None:
        """Commit the irreversible CREATE marker immediately before HTTP.

        A second call for the identity always raises, including after a timeout,
        SEO failure, or crash. Only reconciliation may follow that condition.
        """
        self._validate_identity(rich_article_id, publication_key)
        reservation = self._transaction(
            lambda cursor: self._reserve(cursor, rich_article_id, publication_key, begin=True)
        )
        return reservation.publication_at if reservation is not None else None

    def record_created(self, rich_article_id: int, publication_key: str, wp_post_id: int) -> None:
        """Attach confirmed identity without releasing or moving its slot."""
        self._validate_identity(rich_article_id, publication_key)
        if isinstance(wp_post_id, bool) or not isinstance(wp_post_id, int) or wp_post_id < 1:
            raise ValueError("WordPress post ID must be a positive integer")

        def save(cursor):
            row = self._identity(cursor, rich_article_id, publication_key)
            if row is None or row.get("create_started_at") is None:
                raise CapacityUnavailable("publication CREATE reservation is missing")
            if row.get("wp_post_id") not in (None, wp_post_id):
                raise CapacityUnavailable("publication capacity WordPress identity conflicts")
            cursor.execute(
                """
                UPDATE publication_capacity_reservations
                SET wp_post_id = %s
                WHERE rich_article_id = %s AND publication_key = %s
                """,
                (wp_post_id, rich_article_id, publication_key),
            )

        self._transaction(save)

    @staticmethod
    def _validate_identity(rich_article_id: int, publication_key: str) -> None:
        if isinstance(rich_article_id, bool) or not isinstance(rich_article_id, int) or rich_article_id < 1:
            raise ValueError("rich article ID must be a positive integer")
        if not isinstance(publication_key, str) or not publication_key or len(publication_key) > 191:
            raise ValueError("publication key must contain between 1 and 191 characters")

    def _validate_guard(self, row, *, allow_missing=False) -> None:
        if row.get("hard_cap") is None and not allow_missing:
            raise CapacityUnavailable("publication capacity day guard is invalid")
        if row.get("hard_cap") is not None and int(row["hard_cap"]) != self.limit:
            raise CapacityUnavailable("publication hard cap conflicts with the existing UTC day")
        if int(row.get("baseline_count") or 0) < 0:
            raise CapacityUnavailable("publication capacity baseline is invalid")

    def _ensure_schema(self, cursor) -> None:
        """Refuse nontransactional tables or missing identity/guard constraints.

        Cache only for this repository's lifetime. Deployment must not alter
        these tables while publishers are active; the deployment verifier also
        checks the complete column contract before feature activation.
        """
        if self._schema_checked:
            return
        cursor.execute(
            """
            SELECT TABLE_NAME AS table_name, ENGINE AS engine
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME IN ('publication_capacity_days', 'publication_capacity_reservations')
            """
        )
        engines = {row["table_name"]: str(row["engine"]).lower() for row in cursor.fetchall()}
        expected = {"publication_capacity_days", "publication_capacity_reservations"}
        if set(engines) != expected or any(engine != "innodb" for engine in engines.values()):
            raise CapacityUnavailable("publication capacity requires both InnoDB tables")
        cursor.execute(
            """
            SELECT TABLE_NAME AS table_name, INDEX_NAME AS index_name,
                   NON_UNIQUE AS non_unique, SEQ_IN_INDEX AS position,
                   COLUMN_NAME AS column_name, SUB_PART AS sub_part
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME IN ('publication_capacity_days', 'publication_capacity_reservations')
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """
        )
        indexes: dict[tuple[str, str], list[Any]] = {}
        for row in cursor.fetchall():
            indexes.setdefault((row["table_name"], row["index_name"]), []).append(row)

        def full_index(table: str, columns: tuple[str, ...], *, primary=False, unique=False) -> bool:
            for (indexed_table, name), rows in indexes.items():
                if indexed_table != table or (primary and name != "PRIMARY"):
                    continue
                ordered = sorted(rows, key=lambda row: int(row["position"]))
                if tuple(row["column_name"] for row in ordered) != columns:
                    continue
                if any(row.get("sub_part") is not None for row in ordered):
                    continue
                if (primary or unique) and any(int(row["non_unique"]) != 0 for row in ordered):
                    continue
                return True
            return False

        if not (
            full_index("publication_capacity_days", ("utc_date",), primary=True)
            and full_index("publication_capacity_reservations", ("rich_article_id",), primary=True)
            and full_index("publication_capacity_reservations", ("publication_key",), unique=True)
            and full_index("publication_capacity_reservations", ("utc_date",))
        ):
            raise CapacityUnavailable("publication capacity requires guard and identity indexes")
        self._schema_checked = True

    @staticmethod
    def _now(cursor) -> datetime:
        cursor.execute("SELECT UTC_TIMESTAMP() AS now_utc")
        return cursor.fetchone()["now_utc"]

    def _lock_today(self, cursor) -> tuple[date, datetime, int]:
        day = self._now(cursor).date()
        cursor.execute(
            """
            INSERT INTO publication_capacity_days (`utc_date`, hard_cap, baseline_count)
            VALUES (%s, %s, 0)
            ON DUPLICATE KEY UPDATE `utc_date` = VALUES(`utc_date`)
            """,
            (day, self.limit),
        )
        cursor.execute(
            """
            SELECT `utc_date`, hard_cap, baseline_count
            FROM publication_capacity_days WHERE `utc_date` = %s FOR UPDATE
            """,
            (day,),
        )
        guard = cursor.fetchone()
        if guard is None:
            raise CapacityUnavailable("publication capacity day guard is missing")
        self._validate_guard(guard)
        now = self._now(cursor)
        if now.date() != day:
            raise _DayChanged()
        return day, now, int(guard["baseline_count"])

    @staticmethod
    def _identity(cursor, rich_article_id: int, publication_key: str):
        cursor.execute(
            """
            SELECT rich_article_id, publication_key, `utc_date`,
                   publication_at, create_started_at, wp_post_id
            FROM publication_capacity_reservations
            WHERE rich_article_id = %s OR publication_key = %s
            FOR UPDATE
            """,
            (rich_article_id, publication_key),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1 or rows[0]["rich_article_id"] != rich_article_id or rows[0]["publication_key"] != publication_key:
            raise CapacityUnavailable("publication capacity identity conflicts")
        return rows[0]

    def _reserve(self, cursor, rich_article_id: int, publication_key: str, *, begin: bool):
        day, _locked_at, baseline = self._lock_today(cursor)
        row = self._identity(cursor, rich_article_id, publication_key)
        if row is not None and any(
            row.get(field) is not None for field in ("create_started_at", "publication_at", "wp_post_id")
        ):
            raise CapacityAlreadyAttempted("publication CREATE was already authorized; reconciliation required")

        # A locking read sees the latest committed admissions even under the
        # server's default REPEATABLE READ isolation level.
        cursor.execute(
            """
            SELECT rich_article_id FROM publication_capacity_reservations
            WHERE `utc_date` = %s FOR UPDATE
            """,
            (day,),
        )
        used = baseline + len(cursor.fetchall())
        if used > self.limit:
            raise CapacityUnavailable("publication capacity is overcommitted")
        already_today = row is not None and row["utc_date"] == day
        if not already_today and used >= self.limit:
            return None

        # Refresh after all potentially blocking row locks. A reservation that
        # waited across midnight must be reconsidered against the new day.
        now = self._now(cursor)
        if now.date() != day:
            raise _DayChanged()
        if row is None:
            cursor.execute(
                """
                INSERT INTO publication_capacity_reservations
                    (rich_article_id, publication_key, `utc_date`)
                VALUES (%s, %s, %s)
                """,
                (rich_article_id, publication_key, day),
            )
            row = {
                "rich_article_id": rich_article_id,
                "publication_key": publication_key,
                "utc_date": day,
                "publication_at": None,
                "create_started_at": None,
                "wp_post_id": None,
            }
        elif not already_today:
            cursor.execute(
                """
                UPDATE publication_capacity_reservations SET `utc_date` = %s
                WHERE rich_article_id = %s AND publication_key = %s
                """,
                (day, rich_article_id, publication_key),
            )
            row["utc_date"] = day
        if begin:
            cursor.execute(
                """
                UPDATE publication_capacity_reservations
                SET publication_at = %s, create_started_at = %s
                WHERE rich_article_id = %s AND publication_key = %s
                  AND create_started_at IS NULL
                """,
                (now, now, rich_article_id, publication_key),
            )
            if cursor.rowcount != 1:
                raise CapacityUnavailable("publication CREATE marker was not persisted")
            row.update(publication_at=now, create_started_at=now)
        return CapacityReservation(
            rich_article_id=row["rich_article_id"],
            publication_key=row["publication_key"],
            utc_date=row["utc_date"],
            publication_at=_utc(row.get("publication_at")),
            create_started_at=_utc(row.get("create_started_at")),
            wp_post_id=row.get("wp_post_id"),
        )

    def _transaction(self, operation: Callable[[Any], _T]) -> _T:
        for attempt in range(3):
            conn = None
            cursor = None
            try:
                conn = self._connect()
                conn.start_transaction()
                cursor = conn.cursor(dictionary=True)
                self._ensure_schema(cursor)
                result = operation(cursor)
                conn.commit()
                return result
            except Exception as error:
                if conn is not None:
                    conn.rollback()
                retryable = isinstance(error, _DayChanged) or is_claim_deadlock(error)
                if retryable and attempt < 2:
                    continue
                if isinstance(error, CapacityUnavailable):
                    raise
                raise CapacityUnavailable("publication capacity transaction failed") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if conn is not None:
                    conn.close()
        raise CapacityUnavailable("publication capacity transaction could not complete")
