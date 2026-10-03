"""Read-only verification for application and vector migration artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from .models import ArtifactState, MigrationTarget


@dataclass(frozen=True)
class ExpectedColumn:
    column_type: str
    nullable: bool
    default: str | None | object = ...
    extra_contains: str | None = None
    generation_contains: tuple[str, ...] = ()


def _normalize_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return re.sub(r"\b(tinyint|smallint|int|bigint)\(\d+\)", r"\1", normalized)


def _normalize_default(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().strip("'").lower()
    if normalized == "null":
        return None
    return normalized.removesuffix("()")


class SchemaInspector:
    def __init__(self, connection: Any, database: str):
        self.connection = connection
        self.database = database

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def tables(self) -> set[str]:
        return {
            str(row[0])
            for row in self._rows(
                """
                SELECT TABLE_NAME FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                """,
                (self.database,),
            )
        }

    def columns(self, table: str) -> dict[str, tuple[str, bool, Any, str, str]]:
        rows = self._rows(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
                   EXTRA, GENERATION_EXPRESSION
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """,
            (self.database, table),
        )
        return {
            str(name): (
                _normalize_type(column_type),
                str(nullable).upper() == "YES",
                default,
                str(extra or "").lower(),
                str(generation or "").lower(),
            )
            for name, column_type, nullable, default, extra, generation in rows
        }

    def indexes(self, table: str) -> dict[str, tuple[bool, str, tuple[str, ...]]]:
        rows = self._rows(
            """
            SELECT INDEX_NAME, NON_UNIQUE, INDEX_TYPE, SEQ_IN_INDEX, COLUMN_NAME
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY INDEX_NAME, SEQ_IN_INDEX
            """,
            (self.database, table),
        )
        grouped: dict[str, tuple[bool, str, list[str]]] = {}
        for name, non_unique, index_type, _sequence, column in rows:
            key = str(name)
            if key not in grouped:
                grouped[key] = (not bool(non_unique), str(index_type).upper(), [])
            grouped[key][2].append(str(column))
        return {
            name: (unique, index_type, tuple(columns))
            for name, (unique, index_type, columns) in grouped.items()
        }

    def checks(self, table: str) -> dict[str, str]:
        return {
            str(name): str(clause or "").lower()
            for name, clause in self._rows(
                """
                SELECT CONSTRAINT_NAME, CHECK_CLAUSE
                FROM information_schema.CHECK_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (self.database, table),
            )
        }

    def foreign_keys(self, table: str) -> dict[str, tuple[str, str, str, str]]:
        rows = self._rows(
            """
            SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_NAME,
                   k.REFERENCED_COLUMN_NAME, r.DELETE_RULE
            FROM information_schema.KEY_COLUMN_USAGE k
            JOIN information_schema.TABLE_CONSTRAINTS c
              ON c.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
             AND c.TABLE_NAME = k.TABLE_NAME
             AND c.CONSTRAINT_NAME = k.CONSTRAINT_NAME
            JOIN information_schema.REFERENTIAL_CONSTRAINTS r
              ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
             AND r.TABLE_NAME = k.TABLE_NAME
             AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
            WHERE k.CONSTRAINT_SCHEMA = %s AND k.TABLE_NAME = %s
              AND c.CONSTRAINT_TYPE = 'FOREIGN KEY'
            """,
            (self.database, table),
        )
        return {
            str(name): (
                str(column), str(reference_table), str(reference_column), str(delete_rule).upper()
            )
            for name, column, reference_table, reference_column, delete_rule in rows
        }


APP_PHASE2_RAW_COLUMNS = {
    "processing_status": ExpectedColumn("varchar(16)", False, "pending"),
    "processing_claim_token": ExpectedColumn("char(64)", True, None),
    "processing_claimed_at": ExpectedColumn("datetime", True, None),
    "processing_attempt_count": ExpectedColumn("int unsigned", False, "0"),
    "processing_last_error": ExpectedColumn("varchar(500)", True, None),
}

APP_PHASE2_RICH_COLUMNS = {
    "raw_article_id": ExpectedColumn("int", True, None),
    "publish_status": ExpectedColumn("varchar(16)", False, "pending"),
    "publish_claim_token": ExpectedColumn("char(64)", True, None),
    "publish_claimed_at": ExpectedColumn("datetime", True, None),
    "publish_attempt_count": ExpectedColumn("int unsigned", False, "0"),
    "publish_last_error": ExpectedColumn("varchar(500)", True, None),
    "publication_key": ExpectedColumn("varchar(191)", True, None),
    "wp_post_id": ExpectedColumn("bigint unsigned", True, None),
    "wp_media_id": ExpectedColumn("bigint unsigned", True, None),
    "wp_media_metadata_json": ExpectedColumn("longtext", True, None),
    "wp_post_url": ExpectedColumn("varchar(512)", True, None),
    "wp_post_created_at": ExpectedColumn("datetime", True, None),
    "published_at": ExpectedColumn("datetime", True, None),
}

APP_PHASE2_INDEXES = {
    ("cryptonewsapi", "idx_cryptonewsapi_processing_claim"): (
        False,
        "BTREE",
        ("processed", "chosen_for_publish", "processing_status", "processing_claimed_at"),
    ),
    ("rich_crpytonews", "idx_rich_crpytonews_publish_claim"): (
        False,
        "BTREE",
        ("published", "publish_status", "publish_claimed_at"),
    ),
    ("rich_crpytonews", "uq_rich_crpytonews_raw_article_id"): (
        True,
        "BTREE",
        ("raw_article_id",),
    ),
    ("rich_crpytonews", "uq_rich_crpytonews_publication_key"): (
        True,
        "BTREE",
        ("publication_key",),
    ),
    ("rich_crpytonews", "uq_rich_crpytonews_wp_post_id"): (
        True,
        "BTREE",
        ("wp_post_id",),
    ),
}

APP_DUPLICATE_COLUMNS = {
    "id": ExpectedColumn("bigint unsigned", False, ..., "auto_increment"),
    "article_id": ExpectedColumn("int", False),
    "candidate_article_id": ExpectedColumn("int", False),
    "assessment_type": ExpectedColumn("varchar(32)", False),
    "same_provider_article_id": ExpectedColumn("tinyint(1)", False, "0"),
    "same_event_id": ExpectedColumn("tinyint(1)", False, "0"),
    "same_canonical_url": ExpectedColumn("tinyint(1)", False, "0"),
    "same_content_hash": ExpectedColumn("tinyint(1)", False, "0"),
    "title_token_jaccard": ExpectedColumn("decimal(6,5)", False, "0.00000"),
    "publication_distance_hours": ExpectedColumn("decimal(10,3)", True, None),
    "shared_entities_json": ExpectedColumn("longtext", False),
    "shared_dates_json": ExpectedColumn("longtext", False),
    "shared_numbers_json": ExpectedColumn("longtext", False),
    "reason_json": ExpectedColumn("longtext", False),
    "policy_version": ExpectedColumn("varchar(64)", False),
    "created_at": ExpectedColumn("datetime", False, "current_timestamp"),
    "updated_at": ExpectedColumn("datetime", False, "current_timestamp"),
}

APP_DUPLICATE_INDEXES = {
    "PRIMARY": (True, "BTREE", ("id",)),
    "uq_duplicate_assessment_pair_policy": (
        True,
        "BTREE",
        ("article_id", "candidate_article_id", "policy_version"),
    ),
    "idx_duplicate_assessment_article_created": (
        False,
        "BTREE",
        ("article_id", "created_at"),
    ),
    "idx_duplicate_assessment_candidate_created": (
        False,
        "BTREE",
        ("candidate_article_id", "created_at"),
    ),
}

VECTOR_COLUMNS = {
    "vector_documents": {
        "id": ExpectedColumn("bigint unsigned", False, ..., "auto_increment"),
        "document_key": ExpectedColumn("varchar(191)", False),
        "source_type": ExpectedColumn("varchar(32)", False),
        "source_article_id": ExpectedColumn("int", False),
        "rich_article_id": ExpectedColumn("int", True, None),
        "source_url": ExpectedColumn("varchar(1024)", True, None),
        "title": ExpectedColumn("varchar(512)", False),
        "published_at": ExpectedColumn("datetime", True, None),
        "content_hash": ExpectedColumn("char(64)", False),
        "content_version": ExpectedColumn("varchar(128)", False),
        "created_at": ExpectedColumn("datetime", False, "current_timestamp"),
        "updated_at": ExpectedColumn(
            "datetime", False, "current_timestamp", "on update current_timestamp"
        ),
    },
    "vector_chunks": {
        "id": ExpectedColumn("bigint unsigned", False, ..., "auto_increment"),
        "document_id": ExpectedColumn("bigint unsigned", False),
        "chunk_index": ExpectedColumn("int unsigned", False),
        "chunk_text": ExpectedColumn("mediumtext", False),
        "chunk_hash": ExpectedColumn("char(64)", False),
        "embedding": ExpectedColumn("vector(1536)", False),
        "embedding_model": ExpectedColumn("varchar(191)", False),
        "embedding_dimensions": ExpectedColumn("smallint unsigned", False),
        "embedding_version": ExpectedColumn("varchar(191)", False),
        "created_at": ExpectedColumn("datetime", False, "current_timestamp"),
        "updated_at": ExpectedColumn(
            "datetime", False, "current_timestamp", "on update current_timestamp"
        ),
    },
    "embedding_jobs": {
        "id": ExpectedColumn("bigint unsigned", False, ..., "auto_increment"),
        "document_id": ExpectedColumn("bigint unsigned", False),
        "embedding_version": ExpectedColumn("varchar(191)", False),
        "status": ExpectedColumn("varchar(16)", False, "pending"),
        "claim_token": ExpectedColumn("char(64)", True, None),
        "claimed_at": ExpectedColumn("datetime", True, None),
        "attempt_count": ExpectedColumn("int unsigned", False, "0"),
        "last_error": ExpectedColumn("varchar(500)", True, None),
        "created_at": ExpectedColumn("datetime", False, "current_timestamp"),
        "updated_at": ExpectedColumn(
            "datetime", False, "current_timestamp", "on update current_timestamp"
        ),
    },
}

VECTOR_INDEXES = {
    ("vector_documents", "PRIMARY"): (True, "BTREE", ("id",)),
    ("vector_documents", "uq_vector_documents_key_version"): (
        True, "BTREE", ("document_key", "content_version")
    ),
    ("vector_documents", "idx_vector_documents_source"): (
        False, "BTREE", ("source_type", "source_article_id")
    ),
    ("vector_documents", "idx_vector_documents_rich"): (
        False, "BTREE", ("rich_article_id",)
    ),
    ("vector_documents", "idx_vector_documents_published"): (
        False, "BTREE", ("published_at",)
    ),
    ("vector_chunks", "uq_vector_chunks_position_version"): (
        True, "BTREE", ("document_id", "chunk_index", "embedding_version")
    ),
    ("vector_chunks", "PRIMARY"): (True, "BTREE", ("id",)),
    ("vector_chunks", "uq_vector_chunks_hash_version"): (
        True, "BTREE", ("document_id", "chunk_hash", "embedding_version")
    ),
    ("vector_chunks", "idx_vector_chunks_document_version"): (
        False, "BTREE", ("document_id", "embedding_version", "chunk_index")
    ),
    ("embedding_jobs", "uq_embedding_jobs_document_version"): (
        True, "BTREE", ("document_id", "embedding_version")
    ),
    ("embedding_jobs", "PRIMARY"): (True, "BTREE", ("id",)),
    ("embedding_jobs", "idx_embedding_jobs_claim"): (
        False, "BTREE", ("status", "claimed_at", "id")
    ),
}

SEMANTIC_COLUMNS = {
    "id": ExpectedColumn("bigint unsigned", False, ..., "auto_increment"),
    "query_source_article_id": ExpectedColumn("int", False),
    "query_document_id": ExpectedColumn("bigint unsigned", True, None),
    "query_document_identity": ExpectedColumn(
        "bigint unsigned", True, ..., None, ("ifnull", "query_document_id", "0")
    ),
    "embedding_version": ExpectedColumn("varchar(191)", False),
    "semantic_version": ExpectedColumn("varchar(64)", False),
    "status": ExpectedColumn("varchar(16)", False),
    "reason": ExpectedColumn("varchar(191)", True, None),
    "lookback_hours": ExpectedColumn("smallint unsigned", False),
    "requested_top_k": ExpectedColumn("smallint unsigned", False),
    "query_chunks_available": ExpectedColumn("int unsigned", False, "0"),
    "query_chunks_considered": ExpectedColumn("smallint unsigned", False, "0"),
    "candidate_count": ExpectedColumn("smallint unsigned", False, "0"),
    "best_native_distance": ExpectedColumn("double", True, None),
    "evidence_json": ExpectedColumn("longtext", False),
    "error_type": ExpectedColumn("varchar(128)", True, None),
    "safe_error": ExpectedColumn("varchar(500)", True, None),
    "evaluation_count": ExpectedColumn("int unsigned", False, "1"),
    "created_at": ExpectedColumn("datetime", False, "current_timestamp"),
    "updated_at": ExpectedColumn(
        "datetime", False, "current_timestamp", "on update current_timestamp"
    ),
}

SEMANTIC_INDEXES = {
    "PRIMARY": (True, "BTREE", ("id",)),
    "uq_semantic_shadow_identity": (
        True,
        "BTREE",
        ("query_source_article_id", "query_document_identity", "embedding_version", "semantic_version"),
    ),
    "idx_semantic_shadow_source_version": (
        False,
        "BTREE",
        ("query_source_article_id", "semantic_version", "updated_at"),
    ),
    "idx_semantic_shadow_document_version": (
        False,
        "BTREE",
        ("query_document_id", "embedding_version", "semantic_version"),
    ),
    "idx_semantic_shadow_status_updated": (
        False, "BTREE", ("status", "updated_at")
    ),
}


def _check_columns(
    inspector: SchemaInspector,
    table: str,
    expected: dict[str, ExpectedColumn],
) -> list[str]:
    issues: list[str] = []
    actual = inspector.columns(table)
    for name, contract in expected.items():
        if name not in actual:
            issues.append(f"{table}.{name} is missing")
            continue
        column_type, nullable, default, extra, generation = actual[name]
        expected_type = _normalize_type(contract.column_type)
        if column_type != expected_type:
            issues.append(f"{table}.{name} type is {column_type}, expected {expected_type}")
        if nullable != contract.nullable:
            issues.append(f"{table}.{name} nullability is incompatible")
        if contract.default is not ... and _normalize_default(default) != _normalize_default(contract.default):
            issues.append(f"{table}.{name} default is incompatible")
        if contract.extra_contains and contract.extra_contains not in extra:
            issues.append(f"{table}.{name} is missing {contract.extra_contains}")
        if any(token not in generation for token in contract.generation_contains):
            issues.append(f"{table}.{name} generation expression is incompatible")
    return issues


def _check_indexes(
    inspector: SchemaInspector,
    expected: dict[tuple[str, str], tuple[bool, str, tuple[str, ...]]],
) -> list[str]:
    issues: list[str] = []
    by_table: dict[str, dict[str, tuple[bool, str, tuple[str, ...]]]] = {}
    for table, name in expected:
        by_table.setdefault(table, inspector.indexes(table))
        actual = by_table[table].get(name)
        if actual != expected[(table, name)]:
            issues.append(f"{table}.{name} is missing or incompatible")
    return issues


def _require_named_checks(
    inspector: SchemaInspector,
    table: str,
    expectations: dict[str, tuple[str, ...]],
) -> list[str]:
    issues: list[str] = []
    actual = inspector.checks(table)
    for name, tokens in expectations.items():
        clause = actual.get(name, "")
        if not clause or any(token.lower() not in clause for token in tokens):
            issues.append(f"{table}.{name} CHECK is missing or incompatible")
    return issues


def verify_artifact(inspector: SchemaInspector, verification_key: str) -> list[str]:
    tables = inspector.tables()
    if verification_key == "app_phase2_state":
        issues = []
        for table in ("cryptonewsapi", "rich_crpytonews"):
            if table not in tables:
                issues.append(f"{table} table is missing")
        if issues:
            return issues
        return _check_columns(inspector, "cryptonewsapi", APP_PHASE2_RAW_COLUMNS) + _check_columns(
            inspector, "rich_crpytonews", APP_PHASE2_RICH_COLUMNS
        )

    if verification_key == "app_phase2_indexes":
        return _check_indexes(inspector, APP_PHASE2_INDEXES)

    if verification_key == "app_phase5_duplicate":
        if "duplicate_assessments" not in tables:
            return ["duplicate_assessments table is missing"]
        issues = _check_columns(inspector, "duplicate_assessments", APP_DUPLICATE_COLUMNS)
        issues += _check_indexes(
            inspector,
            {("duplicate_assessments", name): value for name, value in APP_DUPLICATE_INDEXES.items()},
        )
        issues += _require_named_checks(
            inspector,
            "duplicate_assessments",
            {"chk_duplicate_assessment_distinct_articles": ("article_id", "candidate_article_id")},
        )
        return issues

    if verification_key == "vector_core":
        issues = []
        for table in VECTOR_COLUMNS:
            if table not in tables:
                issues.append(f"{table} table is missing")
            else:
                issues += _check_columns(inspector, table, VECTOR_COLUMNS[table])
        issues += _check_indexes(inspector, VECTOR_INDEXES)
        if "vector_chunks" in tables:
            if inspector.foreign_keys("vector_chunks").get("fk_vector_chunks_document") != (
                "document_id", "vector_documents", "id", "CASCADE"
            ):
                issues.append("vector_chunks document foreign key is missing or incompatible")
            issues += _require_named_checks(
                inspector, "vector_chunks", {"chk_vector_chunks_dimensions": ("embedding_dimensions", "1536")}
            )
        if "embedding_jobs" in tables:
            if inspector.foreign_keys("embedding_jobs").get("fk_embedding_jobs_document") != (
                "document_id", "vector_documents", "id", "CASCADE"
            ):
                issues.append("embedding_jobs document foreign key is missing or incompatible")
            issues += _require_named_checks(
                inspector, "embedding_jobs", {"chk_embedding_jobs_status": ("pending", "completed", "failed")}
            )
        if "vector_documents" in tables:
            issues += _require_named_checks(
                inspector,
                "vector_documents",
                {
                    "chk_vector_documents_source_type": ("source_article", "coincourier_generated"),
                    "chk_vector_documents_provenance": ("rich_article_id", "source_type"),
                },
            )
        return issues

    if verification_key == "vector_index":
        actual = inspector.indexes("vector_chunks").get("idx_vector_chunks_embedding_cosine")
        return [] if actual == (False, "VECTOR", ("embedding",)) else [
            "vector_chunks.idx_vector_chunks_embedding_cosine VECTOR index is missing or incompatible"
        ]

    if verification_key == "vector_semantic":
        if "semantic_shadow_assessments" not in tables:
            return ["semantic_shadow_assessments table is missing"]
        issues = _check_columns(inspector, "semantic_shadow_assessments", SEMANTIC_COLUMNS)
        issues += _check_indexes(
            inspector,
            {("semantic_shadow_assessments", name): value for name, value in SEMANTIC_INDEXES.items()},
        )
        if inspector.foreign_keys("semantic_shadow_assessments").get(
            "fk_semantic_shadow_query_document"
        ) != ("query_document_id", "vector_documents", "id", "CASCADE"):
            issues.append("semantic_shadow_assessments document foreign key is missing or incompatible")
        issues += _require_named_checks(
            inspector,
            "semantic_shadow_assessments",
            {
                "chk_semantic_shadow_source_id": ("query_source_article_id", "0"),
                "chk_semantic_shadow_status": ("retrieved", "error", "not_ready"),
                "chk_semantic_shadow_bounds": ("lookback_hours", "requested_top_k", "candidate_count"),
                "chk_semantic_shadow_evidence_json": ("json_valid", "json_length", "candidate_count"),
                "chk_semantic_shadow_result_shape": ("retrieved", "best_native_distance"),
                "chk_semantic_shadow_error_shape": ("error_type", "safe_error"),
            },
        )
        return issues

    return [f"unknown verification contract: {verification_key}"]


def artifact_state(inspector: SchemaInspector, verification_key: str) -> ArtifactState:
    tables = inspector.tables()
    if verification_key == "app_phase2_state":
        present = set(inspector.columns("cryptonewsapi")) & set(APP_PHASE2_RAW_COLUMNS)
        present |= set(inspector.columns("rich_crpytonews")) & set(APP_PHASE2_RICH_COLUMNS)
    elif verification_key == "app_phase2_indexes":
        present = {
            name
            for table, name in APP_PHASE2_INDEXES
            if name in inspector.indexes(table)
        }
    elif verification_key == "app_phase5_duplicate":
        present = {"duplicate_assessments"} if "duplicate_assessments" in tables else set()
    elif verification_key == "vector_core":
        present = set(VECTOR_COLUMNS) & tables
    elif verification_key == "vector_index":
        present = (
            {"idx_vector_chunks_embedding_cosine"}
            if "idx_vector_chunks_embedding_cosine" in inspector.indexes("vector_chunks")
            else set()
        )
    elif verification_key == "vector_semantic":
        present = {"semantic_shadow_assessments"} if "semantic_shadow_assessments" in tables else set()
    else:
        return ArtifactState.PARTIAL
    if not present:
        return ArtifactState.NONE
    return ArtifactState.COMPLETE if not verify_artifact(inspector, verification_key) else ArtifactState.PARTIAL


def verify_target_schema(inspector: SchemaInspector, target: MigrationTarget) -> list[str]:
    keys: Iterable[str]
    if target == MigrationTarget.APP:
        keys = ("app_phase2_state", "app_phase2_indexes", "app_phase5_duplicate")
    else:
        keys = ("vector_core", "vector_index", "vector_semantic")
    issues: list[str] = []
    for key in keys:
        issues.extend(verify_artifact(inspector, key))
    return issues


def verify_vector_schema_contract(connection: Any, database: str) -> None:
    issues = verify_target_schema(SchemaInspector(connection, database), MigrationTarget.VECTOR)
    if issues:
        raise RuntimeError("; ".join(issues))
