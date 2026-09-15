"""Database target guards, SQL execution, ledger, and advisory locking."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import re
from time import monotonic
from typing import Any, Iterator

from .models import LedgerRecord, MigrationSpec, MigrationTarget, TargetInfo


LEDGER_TABLE = "getnewsapi_migration_ledger"
TOOL_VERSION = "phase7c1-v1"


class MigrationSafetyError(RuntimeError):
    pass


class MigrationExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    migration_id: str
    passed: bool
    result_set_counts: tuple[int, ...]
    duration_seconds: float


def _query_one(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise MigrationSafetyError("database identity query returned no result")
        return tuple(row)
    finally:
        cursor.close()


def _table_names(connection: Any, database: str) -> set[str]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT TABLE_NAME FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
            """,
            (database,),
        )
        return {str(row[0]) for row in cursor.fetchall()}
    finally:
        cursor.close()


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        raise MigrationSafetyError("could not determine MariaDB server version")
    return tuple(int(part or 0) for part in match.groups())


def guard_target(
    connection: Any,
    target: MigrationTarget,
    configured_database: str | None,
) -> TargetInfo:
    configured = (configured_database or "").strip()
    if not configured:
        raise MigrationSafetyError("configured database name is required")

    connected, version = _query_one(connection, "SELECT DATABASE(), VERSION()")
    connected_name = str(connected or "").strip()
    version_text = str(version or "").strip()
    if not connected_name:
        raise MigrationSafetyError("connection has no selected database")
    if connected_name != configured:
        raise MigrationSafetyError("configured and connected database names do not match")
    if "mariadb" not in version_text.lower():
        raise MigrationSafetyError("migration tooling requires MariaDB")

    version_parts = _version_tuple(version_text)
    tables = _table_names(connection, connected_name)
    app_markers = {"cryptonewsapi", "rich_crpytonews"}
    vector_markers = {"vector_documents", "vector_chunks", "embedding_jobs"}

    if target == MigrationTarget.APP:
        if version_parts < (10, 4, 0):
            raise MigrationSafetyError("application migrations require MariaDB 10.4 or newer")
        if vector_markers & tables:
            raise MigrationSafetyError("application migration target contains vector schema markers")
        missing = sorted(app_markers - tables)
        if missing:
            raise MigrationSafetyError(
                "application migration target is missing required base tables: " + ", ".join(missing)
            )
    else:
        from vector_store.db import assert_vector_database_name

        try:
            assert_vector_database_name(connected_name)
        except Exception as exc:
            raise MigrationSafetyError(
                "configured vector database name does not match the guarded vector contract"
            ) from exc
        if version_parts[:2] != (11, 8):
            raise MigrationSafetyError("vector migrations require reviewed MariaDB 11.8")
        if app_markers & tables:
            raise MigrationSafetyError("vector migration target contains application schema markers")
        try:
            distance = _query_one(
                connection,
                "SELECT VEC_DISTANCE_COSINE(VEC_FROMTEXT('[1,0]'), VEC_FROMTEXT('[1,0]'))",
            )[0]
        except Exception as exc:
            raise MigrationSafetyError("MariaDB native vector capability check failed") from exc
        if distance is None or abs(float(distance)) > 0.000001:
            raise MigrationSafetyError("MariaDB native vector capability returned an unexpected result")

    return TargetInfo(target, configured, connected_name, version_text)


def execute_script(connection: Any, migration: MigrationSpec, sql: str) -> tuple[int, ...]:
    """Execute connector-parsed SQL and return bounded result-set row counts."""

    cursor = connection.cursor()
    counts: list[int] = []
    try:
        cursor.execute(sql, map_results=True)
        while True:
            if cursor.with_rows:
                count = 0
                while True:
                    batch = cursor.fetchmany(100)
                    if not batch:
                        break
                    count += len(batch)
                counts.append(count)
            if not cursor.nextset():
                break
        return tuple(counts)
    except Exception as exc:
        raise MigrationExecutionError(f"{migration.id} SQL execution failed") from exc
    finally:
        cursor.close()


def run_preflight(connection: Any, migration: MigrationSpec, sql: str) -> PreflightResult:
    started = monotonic()
    cursor = connection.cursor()
    counts: list[int] = []
    try:
        cursor.execute(sql, map_results=True)
        while True:
            if cursor.with_rows:
                rows = list(cursor.fetchall())
                counts.append(len(rows))
            if not cursor.nextset():
                break
    except Exception as exc:
        raise MigrationExecutionError(f"{migration.id} preflight execution failed") from exc
    finally:
        cursor.close()
    return PreflightResult(
        migration_id=migration.id,
        passed=all(count == 0 for count in counts),
        result_set_counts=tuple(counts),
        duration_seconds=monotonic() - started,
    )


def ledger_exists(connection: Any, database: str) -> bool:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """,
            (database, LEDGER_TABLE),
        )
        return int(cursor.fetchone()[0]) == 1
    finally:
        cursor.close()


def read_ledger(
    connection: Any,
    database: str,
    target: MigrationTarget,
) -> dict[str, LedgerRecord]:
    if not ledger_exists(connection, database):
        return {}
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"SELECT migration_id, checksum, tool_version FROM {LEDGER_TABLE} WHERE target = %s",
            (target.value,),
        )
        return {
            str(migration_id): LedgerRecord(str(migration_id), str(checksum), str(tool_version))
            for migration_id, checksum, tool_version in cursor.fetchall()
        }
    finally:
        cursor.close()


def create_ledger(connection: Any) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                target VARCHAR(16) NOT NULL,
                migration_id VARCHAR(64) NOT NULL,
                checksum CHAR(64) NOT NULL,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                tool_version VARCHAR(64) NOT NULL,
                PRIMARY KEY (target, migration_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
            """
        )
    finally:
        cursor.close()


def record_migration(connection: Any, migration: MigrationSpec) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            INSERT INTO {LEDGER_TABLE} (target, migration_id, checksum, tool_version)
            VALUES (%s, %s, %s, %s)
            """,
            (migration.target.value, migration.id, migration.checksum, TOOL_VERSION),
        )
    finally:
        cursor.close()


def _lock_name(target: MigrationTarget, database: str) -> str:
    digest = hashlib.sha256(f"{target.value}:{database}".encode("utf-8")).hexdigest()[:24]
    return f"getnewsapi:migrate:{target.value}:{digest}"


@contextmanager
def migration_lock(
    connection: Any,
    target: MigrationTarget,
    database: str,
    timeout_seconds: int,
) -> Iterator[None]:
    name = _lock_name(target, database)
    acquired = _query_one(connection, "SELECT GET_LOCK(%s, %s)", (name, timeout_seconds))[0]
    if acquired != 1:
        raise MigrationSafetyError(
            f"{target.value} migration lock was not acquired within {timeout_seconds} seconds"
        )
    try:
        yield
    finally:
        try:
            _query_one(connection, "SELECT RELEASE_LOCK(%s)", (name,))
        except Exception:
            # Connection close also releases MariaDB advisory locks.
            pass
