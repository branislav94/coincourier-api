"""Connection and schema guards for the separate MariaDB vector service."""

from __future__ import annotations

from typing import Any

import mysql.connector

from config import VECTOR_DB_CONFIG, VECTOR_ENABLED

from .models import VECTOR_DIMENSIONS


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
    """Reject missing or incompatible Phase 6A vector schemas."""
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        database = assert_vector_database_name(cursor.fetchone()[0])

        cursor.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME IN ('vector_documents','vector_chunks','embedding_jobs')
            """,
            (database,),
        )
        columns = {
            (table_name, column_name): column_type.lower()
            for table_name, column_name, column_type in cursor.fetchall()
        }
        required_columns = {
            ("vector_documents", "document_key"),
            ("vector_documents", "source_type"),
            ("vector_documents", "source_article_id"),
            ("vector_documents", "rich_article_id"),
            ("vector_documents", "content_version"),
            ("vector_chunks", "document_id"),
            ("vector_chunks", "embedding_version"),
            ("embedding_jobs", "document_id"),
            ("embedding_jobs", "status"),
        }
        missing = sorted(required_columns - set(columns))
        if missing:
            raise VectorSchemaError(f"vector schema is missing columns: {missing}")

        embedding_type = columns.get(("vector_chunks", "embedding"))
        if embedding_type != f"vector({VECTOR_DIMENSIONS})":
            raise VectorSchemaError(
                f"vector_chunks.embedding must be vector({VECTOR_DIMENSIONS}), "
                f"found {embedding_type or 'missing'}"
            )

        cursor.execute(
            """
            SELECT INDEX_NAME, INDEX_TYPE
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'vector_chunks'
              AND INDEX_NAME = 'idx_vector_chunks_embedding_cosine'
            """,
            (database,),
        )
        index_row = cursor.fetchone()
        if not index_row or str(index_row[1]).upper() != "VECTOR":
            raise VectorSchemaError("cosine VECTOR index is missing from vector_chunks")
    finally:
        cursor.close()
