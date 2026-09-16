"""Cross-process MariaDB advisory locks for operational conflict groups."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import logging
from typing import Any, Callable, Iterator, Mapping

from .models import DatabaseTarget


logger = logging.getLogger(__name__)
ConnectionFactory = Callable[[DatabaseTarget], Any]
LOCK_NAMESPACE = "getnewsapi:job:v1"


class JobLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobLockAttempt:
    acquired: bool
    name: str
    target: DatabaseTarget
    database: str


def job_lock_name(conflict_group: str, target: DatabaseTarget, database: str) -> str:
    normalized_group = conflict_group.strip().lower().replace("_", "-")
    if not normalized_group or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in normalized_group
    ):
        raise JobLockError("job conflict group is invalid")
    digest = hashlib.sha256(f"{target.value}:{database}".encode("utf-8")).hexdigest()[:12]
    return f"{LOCK_NAMESPACE}:{normalized_group}:{digest}"


def _configured_database(
    environment: Mapping[str, str],
    target: DatabaseTarget,
) -> str:
    field = "DB_NAME" if target is DatabaseTarget.APP else "VECTOR_DB_NAME"
    database = (environment.get(field) or "").strip()
    if not database:
        raise JobLockError(f"{field} is required for job locking")
    return database


def _default_connection(target: DatabaseTarget):
    import mysql.connector

    from config import DB_CONFIG, VECTOR_DB_CONFIG

    settings = DB_CONFIG if target is DatabaseTarget.APP else VECTOR_DB_CONFIG
    return mysql.connector.connect(**settings)


def _query_one(connection: Any, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise JobLockError("job lock database query returned no result")
        return tuple(row)
    finally:
        cursor.close()


@contextmanager
def advisory_job_lock(
    conflict_group: str,
    target: DatabaseTarget,
    *,
    environment: Mapping[str, str],
    timeout_seconds: int,
    connection_factory: ConnectionFactory | None = None,
) -> Iterator[JobLockAttempt]:
    """Hold one connection-bound lock without keeping a SQL transaction open."""

    if not 1 <= timeout_seconds <= 60:
        raise JobLockError("JOB_LOCK_TIMEOUT_SECONDS must be between 1 and 60")
    database = _configured_database(environment, target)
    name = job_lock_name(conflict_group, target, database)
    connection = None
    acquired = False
    try:
        connection = (connection_factory or _default_connection)(target)
        connection.autocommit = True
        connected_database = str(
            _query_one(connection, "SELECT DATABASE()", ())[0] or ""
        ).strip()
        if connected_database != database:
            raise JobLockError("configured and connected job-lock databases do not match")
        acquisition_result = _query_one(
            connection,
            "SELECT GET_LOCK(%s, %s)",
            (name, timeout_seconds),
        )[0]
        if acquisition_result not in (0, 1):
            raise JobLockError("job lock acquisition returned an invalid result")
        acquired = acquisition_result == 1
        yield JobLockAttempt(acquired, name, target, database)
    finally:
        if connection is not None:
            if acquired:
                try:
                    _query_one(connection, "SELECT RELEASE_LOCK(%s)", (name,))
                except Exception:
                    logger.warning(
                        "job lock release query failed group=%s target=%s; connection close will release it",
                        conflict_group,
                        target.value,
                    )
            try:
                connection.close()
            except Exception:
                logger.warning(
                    "job lock connection close failed group=%s target=%s",
                    conflict_group,
                    target.value,
                )
