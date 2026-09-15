"""Connection and schema guards for the separate MariaDB vector service."""

from __future__ import annotations

from typing import Any

import mysql.connector

from config import VECTOR_DB_CONFIG, VECTOR_ENABLED

PRODUCTION_VECTOR_DATABASE = "coincourier_vectors"


class VectorStoreDisabledError(RuntimeError):
    pass


class VectorDatabaseGuardError(RuntimeError):
    pass


class VectorSchemaError(RuntimeError):
    pass


def assert_vector_database_name(database: str | None) -> str:
    name = (database or "").strip()
    is_test = name.startswith(f"{PRODUCTION_VECTOR_DATABASE}_") and name.endswith("_test")
    if name != PRODUCTION_VECTOR_DATABASE and not is_test:
        raise VectorDatabaseGuardError(
            "vector storage requires coincourier_vectors or a guarded *_test variant"
        )
    return name


def connect_vector_db():
    if not VECTOR_ENABLED:
        raise VectorStoreDisabledError("vector storage is disabled")
    assert_vector_database_name(VECTOR_DB_CONFIG.get("database"))
    return mysql.connector.connect(**VECTOR_DB_CONFIG)


def verify_vector_schema(connection: Any) -> None:
    """Reject missing or incompatible vector migrations 001 through 003."""
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        database = assert_vector_database_name(cursor.fetchone()[0])
    finally:
        cursor.close()

    from deployment.verification import verify_vector_schema_contract

    try:
        verify_vector_schema_contract(connection, database)
    except RuntimeError as exc:
        raise VectorSchemaError(str(exc)) from exc
