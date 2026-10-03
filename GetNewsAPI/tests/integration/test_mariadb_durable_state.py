"""Real-MariaDB coverage for the manual Phase 2 and Phase 5 migrations.

The suite is deliberately opt-in and refuses connections outside a loopback
host or to a database whose name does not end in ``_test``. It uses synthetic
rows only and passes explicit connection factories to application repositories.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_DIR = PROJECT_DIR.parent
MIGRATION_DIR = REPOSITORY_DIR / "maintenance" / "migrations"
BASELINE_SQL = REPOSITORY_DIR / "maintenance" / "testing" / "mariadb_phase2_baseline.sql"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

RUN_INTEGRATION = os.getenv("RUN_MARIADB_INTEGRATION", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DB_HOST = os.getenv("MARIADB_TEST_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("MARIADB_TEST_PORT", "13307"))
DB_NAME = os.getenv("MARIADB_TEST_DATABASE", "coincourier_api_test")
DB_USER = os.getenv("MARIADB_TEST_USER", "getnewsapi_test")
DB_PASSWORD = os.getenv("MARIADB_TEST_PASSWORD", "getnewsapi_test_only")
EXPECTED_VERSION_PREFIX = os.getenv("MARIADB_TEST_VERSION_PREFIX", "10.4.")

PHASE2_RAW_COLUMNS = {
    "processing_status": "varchar(16)",
    "processing_claim_token": "char(64)",
    "processing_claimed_at": "datetime",
    "processing_attempt_count": "int(10) unsigned",
    "processing_last_error": "varchar(500)",
}
PHASE2_RICH_COLUMNS = {
    "raw_article_id": "int(11)",
    "publish_status": "varchar(16)",
    "publish_claim_token": "char(64)",
    "publish_claimed_at": "datetime",
    "publish_attempt_count": "int(10) unsigned",
    "publish_last_error": "varchar(500)",
    "publication_key": "varchar(191)",
    "wp_post_id": "bigint(20) unsigned",
    "wp_media_id": "bigint(20) unsigned",
    "wp_media_metadata_json": "longtext",
    "wp_post_url": "varchar(512)",
    "wp_post_created_at": "datetime",
    "published_at": "datetime",
}
PHASE2_INDEXES = {
    "idx_cryptonewsapi_processing_claim",
    "idx_rich_crpytonews_publish_claim",
    "uq_rich_crpytonews_raw_article_id",
    "uq_rich_crpytonews_publication_key",
    "uq_rich_crpytonews_wp_post_id",
}


def _assert_disposable_target() -> None:
    if DB_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("MariaDB integration tests require a loopback host")
    if not DB_NAME.endswith("_test"):
        raise RuntimeError("MariaDB integration database name must end in '_test'")


def _connect(*, autocommit: bool = False):
    _assert_disposable_target()
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        autocommit=autocommit,
        connection_timeout=10,
    )


def _execute_script(path: Path) -> list[list[tuple[Any, ...]]]:
    """Execute a checked-in test/migration script with connector SQL parsing."""
    connection = _connect(autocommit=True)
    cursor = connection.cursor()
    result_sets: list[list[tuple[Any, ...]]] = []
    try:
        cursor.execute(path.read_text(encoding="utf-8"), map_results=True)
        while True:
            if cursor.with_rows:
                result_sets.append(list(cursor.fetchall()))
            if not cursor.nextset():
                break
        return result_sets
    finally:
        cursor.close()
        connection.close()


class _TrackedConnection:
    def __init__(self) -> None:
        self.connection = _connect()
        self.closed = False

    def close(self) -> None:
        self.connection.close()
        self.closed = True

    def __getattr__(self, name: str):
        return getattr(self.connection, name)


@unittest.skipUnless(
    RUN_INTEGRATION,
    "set RUN_MARIADB_INTEGRATION=true to use the disposable local MariaDB",
)
class MariaDBIntegrationCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        _execute_script(BASELINE_SQL)

    def apply(self, filename: str) -> list[list[tuple[Any, ...]]]:
        return _execute_script(MIGRATION_DIR / filename)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        connection = _connect(autocommit=True)
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return cursor.rowcount
        finally:
            cursor.close()
            connection.close()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        connection = _connect(autocommit=True)
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
        finally:
            cursor.close()
            connection.close()

    def row(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
        rows = self.rows(sql, params)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def column_types(self, table: str) -> dict[str, str]:
        return {
            name: column_type
            for name, column_type in self.rows(
                """
                SELECT COLUMN_NAME, COLUMN_TYPE
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
        }

    def index_names(self, table: str) -> set[str]:
        return {
            name
            for (name,) in self.rows(
                """
                SELECT DISTINCT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
        }

    def assert_phase2_indexes_absent(self) -> None:
        actual = self.index_names("cryptonewsapi") | self.index_names("rich_crpytonews")
        self.assertTrue(PHASE2_INDEXES.isdisjoint(actual))

    def insert_raw(
        self,
        row_id: int,
        url: str,
        *,
        processed: int = 0,
        chosen: int = 1,
        breaking: int = 0,
    ) -> None:
        self.execute(
            """
            INSERT INTO cryptonewsapi
                (id, news_url, canonical_url, title, full_text, publish_date,
                 source_name, topics, tickers, processed, news_id, event_id,
                 title_hash, chosen_for_publish, selected_at, scheduled_for,
                 is_breaking)
            VALUES
                (%s, %s, %s, %s, %s, UTC_TIMESTAMP(),
                 'Synthetic Wire', 'testing', 'TST', %s, %s, %s,
                 SHA2(%s, 256), %s, UTC_TIMESTAMP(),
                 TIMESTAMPADD(MINUTE, -5, UTC_TIMESTAMP()), %s)
            """,
            (
                row_id,
                url,
                url,
                f"Synthetic raw article {row_id}",
                f"Synthetic content for raw article {row_id}.",
                processed,
                f"synthetic-news-{row_id}",
                f"synthetic-event-{row_id}",
                f"Synthetic raw article {row_id}",
                chosen,
                breaking,
            ),
        )

    def insert_rich(self, row_id: int, url: str, *, published: int = 0) -> None:
        self.execute(
            """
            INSERT INTO rich_crpytonews
                (id, news_url, title, full_text, publish_date, source_name,
                 category, hashtags, sentiment, tickers, image_url,
                 seo_focus, seo_slug, seo_meta, published)
            VALUES
                (%s, %s, %s, %s, UTC_TIMESTAMP(), 'Synthetic Wire',
                 'Markets', 'Synthetic, Testing', 0, 'TST',
                 'https://images.example.test/synthetic.jpg',
                 'synthetic integration', %s,
                 'Synthetic metadata for a disposable integration fixture.', %s)
            """,
            (
                row_id,
                url,
                f"Synthetic rich article {row_id}",
                f"<p>Synthetic content for rich article {row_id}.</p>",
                f"synthetic-rich-{row_id}",
                published,
            ),
        )

    def load_happy_fixtures(self) -> None:
        self.insert_raw(1, "https://example.test/raw-unprocessed", processed=0, breaking=0)
        self.insert_raw(2, "https://example.test/raw-processed", processed=1, breaking=1)
        self.insert_rich(101, "https://example.test/raw-unprocessed", published=0)
        self.insert_rich(102, "https://example.test/raw-processed", published=1)


class MigrationHappyPathTests(MariaDBIntegrationCase):
    def test_phase2_phase5_happy_path_and_documented_reruns(self) -> None:
        self.load_happy_fixtures()

        identity_results = self.apply("001_phase2_identity_preflight.sql")
        self.assertEqual(identity_results, [[], [], []])

        self.apply("002_phase2_durable_state.sql")
        raw_columns = self.column_types("cryptonewsapi")
        rich_columns = self.column_types("rich_crpytonews")
        for name, column_type in PHASE2_RAW_COLUMNS.items():
            self.assertEqual(raw_columns[name], column_type)
        for name, column_type in PHASE2_RICH_COLUMNS.items():
            self.assertEqual(rich_columns[name], column_type)

        self.assertEqual(
            self.rows(
                """
                SELECT id, processed, processing_status
                FROM cryptonewsapi ORDER BY id
                """
            ),
            [(1, 0, "pending"), (2, 1, "completed")],
        )
        self.assertEqual(
            self.rows(
                """
                SELECT id, raw_article_id, publication_key, published, publish_status
                FROM rich_crpytonews ORDER BY id
                """
            ),
            [
                (101, 1, "coincourier:101:1", 0, "pending"),
                (102, 2, "coincourier:102:2", 1, "published"),
            ],
        )

        uniqueness_results = self.apply("003_phase2_uniqueness_preflight.sql")
        self.assertEqual(uniqueness_results, [[], [], []])
        self.apply("004_phase2_indexes.sql")
        actual_indexes = self.index_names("cryptonewsapi") | self.index_names(
            "rich_crpytonews"
        )
        self.assertTrue(PHASE2_INDEXES.issubset(actual_indexes))

        phase5_results = self.apply("006_phase5_duplicate_preflight.sql")
        self.assertEqual(phase5_results, [[], []])
        self.apply("007_phase5_duplicate_shadow.sql")
        self.assertEqual(self.row("SELECT COUNT(*) FROM duplicate_assessments"), (0,))

        version = self.row("SELECT VERSION()")[0]
        self.assertTrue(str(version).startswith(EXPECTED_VERSION_PREFIX), version)
        self.assertEqual(
            self.rows(
                """
                SELECT TABLE_NAME, ENGINE, TABLE_COLLATION
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN ('cryptonewsapi','rich_crpytonews')
                ORDER BY TABLE_NAME
                """
            ),
            [
                ("cryptonewsapi", "InnoDB", "utf8mb4_general_ci"),
                ("rich_crpytonews", "InnoDB", "utf8mb4_general_ci"),
            ],
        )
        self.assertEqual(
            self.row("SELECT SHA2('abc', 256)")[0],
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(
            self.row("SELECT TIMESTAMPADD(MINUTE, -30, UTC_TIMESTAMP()) IS NOT NULL"),
            (1,),
        )
        connection = _connect()
        cursor = connection.cursor()
        try:
            connection.start_transaction()
            cursor.execute("SELECT id FROM cryptonewsapi WHERE id=1 FOR UPDATE")
            self.assertEqual(cursor.fetchone(), (1,))
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

        self.apply("002_phase2_durable_state.sql")
        self.apply("004_phase2_indexes.sql")
        self.apply("007_phase5_duplicate_shadow.sql")
        self.assertEqual(self.apply("001_phase2_identity_preflight.sql"), [[], [], []])
        self.assertEqual(self.apply("003_phase2_uniqueness_preflight.sql"), [[], [], []])
        self.assertEqual(self.row("SELECT COUNT(*) FROM duplicate_assessments"), (0,))
        rerun_phase5_preflight = self.apply("006_phase5_duplicate_preflight.sql")
        self.assertEqual(rerun_phase5_preflight[0], [("duplicate_assessments",)])
        self.assertEqual(rerun_phase5_preflight[1], [])

        connection = _connect(autocommit=True)
        cursor = connection.cursor()
        try:
            with self.assertRaises(mysql.connector.DatabaseError):
                cursor.execute(
                    """
                    INSERT INTO duplicate_assessments
                        (article_id, candidate_article_id, assessment_type,
                         shared_entities_json, shared_dates_json,
                         shared_numbers_json, reason_json, policy_version)
                    VALUES (1, 1, 'exact_duplicate', '[]', '[]', '[]', '{}', 'v1')
                    """
                )
        finally:
            cursor.close()
            connection.close()


class AdversarialPreflightTests(MariaDBIntegrationCase):
    def test_001_detects_duplicate_raw_url_after_schema_drift(self) -> None:
        self.execute("ALTER TABLE cryptonewsapi DROP INDEX uq_cryptonewsapi_news_url")
        self.insert_raw(1, "https://duplicates.example.test/raw")
        self.insert_raw(2, "https://duplicates.example.test/raw")
        results = self.apply("001_phase2_identity_preflight.sql")
        self.assertEqual(len(results[0]), 1)
        self.assertEqual(results[0][0][1], 2)
        self.assert_phase2_indexes_absent()

    def test_001_detects_duplicate_rich_url_after_schema_drift(self) -> None:
        self.execute("ALTER TABLE rich_crpytonews DROP INDEX uq_rich_crpytonews_news_url")
        url = "https://duplicates.example.test/rich"
        self.insert_raw(1, url)
        self.insert_rich(101, url)
        self.insert_rich(102, url)
        results = self.apply("001_phase2_identity_preflight.sql")
        self.assertEqual(len(results[1]), 1)
        self.assertEqual(results[1][0][1], 2)
        self.assert_phase2_indexes_absent()

    def test_001_detects_orphan_rich_row(self) -> None:
        self.insert_rich(101, "https://orphans.example.test/rich")
        results = self.apply("001_phase2_identity_preflight.sql")
        self.assertEqual(results[2][0][0], 101)
        self.assertEqual(results[2][0][2], 0)
        self.assert_phase2_indexes_absent()

    def test_001_detects_ambiguous_mapping_after_schema_drift(self) -> None:
        self.execute("ALTER TABLE cryptonewsapi DROP INDEX uq_cryptonewsapi_news_url")
        url = "https://ambiguous.example.test/story"
        self.insert_raw(1, url)
        self.insert_raw(2, url)
        self.insert_rich(101, url)
        results = self.apply("001_phase2_identity_preflight.sql")
        self.assertEqual(results[2][0][0], 101)
        self.assertEqual(results[2][0][2], 2)
        self.assert_phase2_indexes_absent()

    def _prepare_phase2_collision_fixture(self) -> None:
        self.load_happy_fixtures()
        self.assertEqual(self.apply("001_phase2_identity_preflight.sql"), [[], [], []])
        self.apply("002_phase2_durable_state.sql")

    def test_003_detects_publication_key_collision(self) -> None:
        self._prepare_phase2_collision_fixture()
        self.execute("UPDATE rich_crpytonews SET publication_key='synthetic-collision'")
        results = self.apply("003_phase2_uniqueness_preflight.sql")
        self.assertEqual(results[0], [("synthetic-collision", 2)])
        self.assert_phase2_indexes_absent()

    def test_003_detects_raw_article_id_collision(self) -> None:
        self._prepare_phase2_collision_fixture()
        self.execute("UPDATE rich_crpytonews SET raw_article_id=1")
        results = self.apply("003_phase2_uniqueness_preflight.sql")
        self.assertEqual(results[1], [(1, 2)])
        self.assert_phase2_indexes_absent()

    def test_003_detects_wp_post_id_collision(self) -> None:
        self._prepare_phase2_collision_fixture()
        self.execute("UPDATE rich_crpytonews SET wp_post_id=7001")
        results = self.apply("003_phase2_uniqueness_preflight.sql")
        self.assertEqual(results[2], [(7001, 2)])
        self.assert_phase2_indexes_absent()


class PartialMigrationAndRollbackTests(MariaDBIntegrationCase):
    def test_002_rerun_completes_partial_compatible_schema_and_preserves_identity(self) -> None:
        self.execute(
            "ALTER TABLE cryptonewsapi ADD processing_status VARCHAR(16) NOT NULL DEFAULT 'pending'"
        )
        self.execute("ALTER TABLE rich_crpytonews ADD publication_key VARCHAR(191) NULL")
        self.load_happy_fixtures()
        self.execute(
            "UPDATE rich_crpytonews SET publication_key='operator-preserved-key' WHERE id=101"
        )

        self.apply("002_phase2_durable_state.sql")
        self.assertTrue(PHASE2_RAW_COLUMNS.keys() <= self.column_types("cryptonewsapi").keys())
        self.assertTrue(PHASE2_RICH_COLUMNS.keys() <= self.column_types("rich_crpytonews").keys())
        self.assertEqual(
            self.row("SELECT processing_status FROM cryptonewsapi WHERE id=2"),
            ("completed",),
        )
        self.assertEqual(
            self.rows(
                "SELECT id, publication_key FROM rich_crpytonews ORDER BY id"
            ),
            [(101, "operator-preserved-key"), (102, "coincourier:102:2")],
        )

    def test_if_not_exists_does_not_validate_an_incompatible_existing_type(self) -> None:
        self.execute(
            "ALTER TABLE cryptonewsapi ADD processing_status INT NOT NULL DEFAULT 0"
        )
        self.apply("002_phase2_durable_state.sql")
        actual_type = self.column_types("cryptonewsapi")["processing_status"]
        self.assertEqual(actual_type, "int(11)")
        self.assertNotEqual(actual_type, PHASE2_RAW_COLUMNS["processing_status"])

    def test_005_releases_claims_without_removing_schema_or_external_ids(self) -> None:
        self.load_happy_fixtures()
        self.apply("002_phase2_durable_state.sql")
        self.assertEqual(self.apply("003_phase2_uniqueness_preflight.sql"), [[], [], []])
        self.apply("004_phase2_indexes.sql")
        before_raw_columns = self.column_types("cryptonewsapi")
        before_rich_columns = self.column_types("rich_crpytonews")
        before_indexes = self.index_names("cryptonewsapi") | self.index_names(
            "rich_crpytonews"
        )

        self.execute(
            """
            UPDATE cryptonewsapi
            SET processing_status='claimed', processing_claim_token=REPEAT('a',64),
                processing_claimed_at=UTC_TIMESTAMP()
            """
        )
        self.execute(
            """
            UPDATE rich_crpytonews
            SET publish_status=CASE WHEN id=101 THEN 'claimed' ELSE 'post_created' END,
                publish_claim_token=REPEAT('b',64),
                publish_claimed_at=UTC_TIMESTAMP(),
                wp_post_id=CASE WHEN id=101 THEN 7101 ELSE 7102 END,
                wp_media_id=CASE WHEN id=101 THEN 8101 ELSE 8102 END
            """
        )

        self.apply("005_phase2_rollback_state.sql")
        self.assertEqual(
            self.rows(
                """
                SELECT id, processed, processing_status,
                       processing_claim_token, processing_claimed_at
                FROM cryptonewsapi ORDER BY id
                """
            ),
            [(1, 0, "retryable", None, None), (2, 1, "completed", None, None)],
        )
        self.assertEqual(
            self.rows(
                """
                SELECT id, published, publish_status, publish_claim_token,
                       publish_claimed_at, wp_post_id, wp_media_id
                FROM rich_crpytonews ORDER BY id
                """
            ),
            [
                (101, 0, "retryable", None, None, 7101, 8101),
                (102, 1, "published", None, None, 7102, 8102),
            ],
        )
        self.assertEqual(self.column_types("cryptonewsapi"), before_raw_columns)
        self.assertEqual(self.column_types("rich_crpytonews"), before_rich_columns)
        self.assertEqual(
            self.index_names("cryptonewsapi") | self.index_names("rich_crpytonews"),
            before_indexes,
        )


class RepositoryIntegrationTests(MariaDBIntegrationCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        os.environ["DB_HOST"] = DB_HOST
        os.environ["DB_PORT"] = str(DB_PORT)
        os.environ["DB_NAME"] = DB_NAME
        os.environ["DB_USER"] = DB_USER
        os.environ["DB_PASSWORD"] = DB_PASSWORD
        from repositories.publication import PublicationRepository
        from repositories.raw_news import RawNewsRepository

        cls.PublicationRepository = PublicationRepository
        cls.RawNewsRepository = RawNewsRepository

    def setUp(self) -> None:
        super().setUp()
        self.apply("002_phase2_durable_state.sql")
        self.apply("004_phase2_indexes.sql")

    @staticmethod
    def repository_connect():
        return _connect()

    def reset_repository_rows(self) -> None:
        self.execute("DELETE FROM rich_crpytonews")
        self.execute("DELETE FROM cryptonewsapi")

    @staticmethod
    def simultaneous_claims(worker_count: int, claim):
        barrier = threading.Barrier(worker_count)

        def synchronized_claim():
            barrier.wait(timeout=10)
            return claim()

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(synchronized_claim) for _ in range(worker_count)]
            return [future.result(timeout=20) for future in futures]

    def assert_row_locks_released(self, table: str) -> None:
        connection = _connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
            connection.start_transaction()
            cursor.execute(f"SELECT id FROM {table} ORDER BY id FOR UPDATE")
            cursor.fetchall()
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

    def test_two_real_processing_workers_claim_one_row_once(self) -> None:
        self.insert_raw(1, "https://repository.example.test/raw-race")
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            return self.RawNewsRepository(connect=self.repository_connect).claim_next(
                timeout_minutes=30,
                lookahead_minutes=None,
                fresh_start_after=None,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _index: claim(), range(2)))
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertEqual(
            self.row(
                "SELECT processing_status, processing_attempt_count FROM cryptonewsapi WHERE id=1"
            ),
            ("claimed", 1),
        )

    def test_processing_active_claim_expiry_and_owner_tokens(self) -> None:
        self.insert_raw(1, "https://repository.example.test/raw-expiry")
        repository = self.RawNewsRepository(connect=self.repository_connect)
        first = repository.claim_next(
            timeout_minutes=30,
            lookahead_minutes=None,
            fresh_start_after=None,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(
            repository.claim_next(
                timeout_minutes=30,
                lookahead_minutes=None,
                fresh_start_after=None,
            )
        )
        self.execute(
            """
            UPDATE cryptonewsapi
            SET processing_claimed_at=TIMESTAMPADD(MINUTE,-31,UTC_TIMESTAMP())
            WHERE id=1
            """
        )
        recovered = repository.claim_next(
            timeout_minutes=30,
            lookahead_minutes=None,
            fresh_start_after=None,
        )
        self.assertIsNotNone(recovered)
        assert first is not None and recovered is not None
        self.assertTrue(recovered.recovered)
        self.assertNotEqual(first.token, recovered.token)
        self.assertFalse(repository.complete(1, first.token))
        self.assertFalse(repository.complete(1, "wrong-token"))
        self.assertTrue(repository.complete(1, recovered.token))
        self.assertEqual(
            self.row(
                "SELECT processed, processing_status, processing_attempt_count FROM cryptonewsapi WHERE id=1"
            ),
            (1, "completed", 2),
        )

    def test_two_real_publishing_workers_claim_one_row_once(self) -> None:
        url = "https://repository.example.test/publish-race"
        self.insert_raw(1, url)
        self.insert_rich(101, url)
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            return self.PublicationRepository(connect=self.repository_connect).claim_next(
                timeout_minutes=30,
                fresh_start_after=None,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _index: claim(), range(2)))
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertEqual(
            self.row(
                "SELECT publish_status, publish_attempt_count FROM rich_crpytonews WHERE id=101"
            ),
            ("claimed", 1),
        )

    def test_deadlock_errno_recognition_is_1213_only(self) -> None:
        from repositories.state import is_claim_deadlock

        deadlock = mysql.connector.errors.InternalError(
            msg="deadlock", errno=1213, sqlstate="40001"
        )
        lock_timeout = mysql.connector.errors.DatabaseError(
            msg="lock timeout", errno=1205, sqlstate="HY000"
        )
        self.assertTrue(is_claim_deadlock(deadlock))
        self.assertFalse(is_claim_deadlock(lock_timeout))

    def test_raw_lock_wait_timeout_propagates_and_releases_transaction(self) -> None:
        self.insert_raw(1, "https://repository.example.test/raw-lock-timeout")
        blocker = _connect()
        blocker_cursor = blocker.cursor()
        tracked: list[_TrackedConnection] = []

        def timeout_connect():
            connection = _TrackedConnection()
            cursor = connection.cursor()
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
            cursor.close()
            tracked.append(connection)
            return connection

        try:
            blocker.start_transaction()
            blocker_cursor.execute("SELECT id FROM cryptonewsapi WHERE id=1 FOR UPDATE")
            self.assertEqual(blocker_cursor.fetchone(), (1,))
            repository = self.RawNewsRepository(connect=timeout_connect)
            with self.assertRaises(mysql.connector.Error) as raised:
                repository.claim_next(
                    timeout_minutes=30,
                    lookahead_minutes=None,
                    fresh_start_after=None,
                )
            self.assertEqual(raised.exception.errno, 1205)
            self.assertTrue(tracked[-1].closed)
        finally:
            blocker.rollback()
            blocker_cursor.close()
            blocker.close()

        claim = self.RawNewsRepository(connect=self.repository_connect).claim_next(
            timeout_minutes=30,
            lookahead_minutes=None,
            fresh_start_after=None,
        )
        self.assertIsNotNone(claim)
        self.assertEqual(
            self.row(
                "SELECT processing_status, processing_attempt_count FROM cryptonewsapi WHERE id=1"
            ),
            ("claimed", 1),
        )

    def test_raw_schema_error_propagates_after_transaction_cleanup(self) -> None:
        self.execute("DROP TABLE rich_crpytonews")
        self.execute("DROP TABLE cryptonewsapi")
        tracked: list[_TrackedConnection] = []

        def tracked_connect():
            connection = _TrackedConnection()
            tracked.append(connection)
            return connection

        repository = self.RawNewsRepository(connect=tracked_connect)
        with self.assertRaises(mysql.connector.Error) as raised:
            repository.claim_next(
                timeout_minutes=30,
                lookahead_minutes=None,
                fresh_start_after=None,
            )
        self.assertEqual(raised.exception.errno, 1146)
        self.assertTrue(tracked[-1].closed)

    def test_publication_schema_error_propagates_after_transaction_cleanup(self) -> None:
        self.execute("DROP TABLE rich_crpytonews")
        tracked: list[_TrackedConnection] = []

        def tracked_connect():
            connection = _TrackedConnection()
            tracked.append(connection)
            return connection

        repository = self.PublicationRepository(connect=tracked_connect)
        with self.assertRaises(mysql.connector.Error) as raised:
            repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        self.assertEqual(raised.exception.errno, 1146)
        self.assertTrue(tracked[-1].closed)

    def test_twenty_raw_single_row_claim_races(self) -> None:
        for race_number in range(20):
            with self.subTest(race=race_number + 1):
                self.reset_repository_rows()
                self.insert_raw(
                    1,
                    f"https://repository.example.test/raw-stress/{race_number}",
                )
                claims = self.simultaneous_claims(
                    2,
                    lambda: self.RawNewsRepository(
                        connect=self.repository_connect
                    ).claim_next(
                        timeout_minutes=30,
                        lookahead_minutes=None,
                        fresh_start_after=None,
                    ),
                )
                winners = [claim for claim in claims if claim is not None]
                self.assertEqual(len(winners), 1)
                self.assertEqual(
                    self.row(
                        """
                        SELECT processing_status, processing_attempt_count,
                               processing_claim_token = %s
                        FROM cryptonewsapi WHERE id=1
                        """,
                        (winners[0].token,),
                    ),
                    ("claimed", 1, 1),
                )
                self.assert_row_locks_released("cryptonewsapi")

    def test_twenty_publication_single_row_claim_races(self) -> None:
        for race_number in range(20):
            with self.subTest(race=race_number + 1):
                self.reset_repository_rows()
                url = f"https://repository.example.test/publication-stress/{race_number}"
                self.insert_raw(1, url)
                self.insert_rich(101, url)
                claims = self.simultaneous_claims(
                    2,
                    lambda: self.PublicationRepository(
                        connect=self.repository_connect
                    ).claim_next(timeout_minutes=30, fresh_start_after=None),
                )
                winners = [claim for claim in claims if claim is not None]
                self.assertEqual(len(winners), 1)
                self.assertEqual(
                    self.row(
                        """
                        SELECT publish_status, publish_attempt_count,
                               publish_claim_token = %s
                        FROM rich_crpytonews WHERE id=101
                        """,
                        (winners[0].token,),
                    ),
                    ("claimed", 1, 1),
                )
                self.assert_row_locks_released("rich_crpytonews")

    def test_five_raw_rows_with_ten_concurrent_claimers(self) -> None:
        for row_id in range(1, 6):
            self.insert_raw(
                row_id,
                f"https://repository.example.test/raw-multi/{row_id}",
            )
        claims = self.simultaneous_claims(
            10,
            lambda: self.RawNewsRepository(connect=self.repository_connect).claim_next(
                timeout_minutes=30,
                lookahead_minutes=None,
                fresh_start_after=None,
            ),
        )
        winners = [claim for claim in claims if claim is not None]
        winner_ids = [claim.article["id"] for claim in winners]
        self.assertEqual(len(winners), 5)
        self.assertEqual(len(winner_ids), len(set(winner_ids)))
        self.assertEqual(
            self.row(
                """
                SELECT COUNT(*), COALESCE(SUM(processing_attempt_count), 0),
                       COUNT(DISTINCT processing_claim_token)
                FROM cryptonewsapi WHERE processing_status='claimed'
                """
            ),
            (len(winners), len(winners), len(winners)),
        )
        for claim in winners:
            self.assertEqual(
                self.row(
                    "SELECT processing_claim_token = %s FROM cryptonewsapi WHERE id=%s",
                    (claim.token, claim.article["id"]),
                ),
                (1,),
            )
        self.assertEqual(
            self.row(
                """
                SELECT COUNT(*) FROM cryptonewsapi
                WHERE processed=0 AND chosen_for_publish=1
                  AND processing_status IN ('pending', 'retryable')
                """
            ),
            (0,),
        )
        self.assert_row_locks_released("cryptonewsapi")

    def test_five_publication_rows_with_ten_concurrent_claimers(self) -> None:
        for offset in range(5):
            raw_id = offset + 1
            rich_id = offset + 101
            url = f"https://repository.example.test/publication-multi/{raw_id}"
            self.insert_raw(raw_id, url)
            self.insert_rich(rich_id, url)
        claims = self.simultaneous_claims(
            10,
            lambda: self.PublicationRepository(
                connect=self.repository_connect
            ).claim_next(timeout_minutes=30, fresh_start_after=None),
        )
        winners = [claim for claim in claims if claim is not None]
        winner_ids = [claim.article["id"] for claim in winners]
        self.assertEqual(len(winners), 5)
        self.assertEqual(len(winner_ids), len(set(winner_ids)))
        self.assertEqual(
            self.row(
                """
                SELECT COUNT(*), COALESCE(SUM(publish_attempt_count), 0),
                       COUNT(DISTINCT publish_claim_token)
                FROM rich_crpytonews WHERE publish_status='claimed'
                """
            ),
            (len(winners), len(winners), len(winners)),
        )
        for claim in winners:
            self.assertEqual(
                self.row(
                    "SELECT publish_claim_token = %s FROM rich_crpytonews WHERE id=%s",
                    (claim.token, claim.article["id"]),
                ),
                (1,),
            )
        self.assertEqual(
            self.row(
                """
                SELECT COUNT(*) FROM rich_crpytonews
                WHERE published=0
                  AND publish_status IN ('pending', 'retryable')
                """
            ),
            (0,),
        )
        self.assert_row_locks_released("rich_crpytonews")

    def test_publication_expiry_identity_media_post_and_owner_tokens(self) -> None:
        url = "https://repository.example.test/publish-state"
        self.insert_raw(1, url)
        self.insert_rich(101, url)
        repository = self.PublicationRepository(connect=self.repository_connect)
        first = repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertIsNone(repository.claim_next(timeout_minutes=30, fresh_start_after=None))
        self.assertTrue(
            repository.save_identity(101, first.token, 1, "coincourier:101:1")
        )
        self.assertTrue(
            repository.save_media(
                101,
                first.token,
                8101,
                {"provider": "synthetic", "fixture": True},
            )
        )
        self.assertTrue(
            repository.save_external_post(
                101,
                first.token,
                7101,
                "https://wordpress.example.test/posts/7101",
            )
        )
        self.assertEqual(
            self.row(
                "SELECT publish_status, wp_media_id, wp_post_id FROM rich_crpytonews WHERE id=101"
            ),
            ("post_created", 8101, 7101),
        )
        self.execute(
            """
            UPDATE rich_crpytonews
            SET publish_claimed_at=TIMESTAMPADD(MINUTE,-31,UTC_TIMESTAMP())
            WHERE id=101
            """
        )
        recovered = repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertTrue(recovered.recovered)
        self.assertNotEqual(first.token, recovered.token)
        self.assertFalse(repository.complete(101, first.token))
        self.assertFalse(repository.complete(101, "wrong-token"))
        self.assertTrue(repository.complete(101, recovered.token))
        published, status, media_id, post_id, metadata_json = self.row(
            """
            SELECT published, publish_status, wp_media_id, wp_post_id,
                   wp_media_metadata_json
            FROM rich_crpytonews WHERE id=101
            """
        )
        self.assertEqual((published, status, media_id, post_id), (1, "published", 8101, 7101))
        self.assertEqual(
            json.loads(metadata_json),
            {"fixture": True, "provider": "synthetic"},
        )

    def test_claim_connections_close_and_release_locks_before_application_work(self) -> None:
        url = "https://repository.example.test/transaction-boundary"
        self.insert_raw(1, url)
        self.insert_rich(101, url)
        tracked: list[_TrackedConnection] = []

        def tracked_connect():
            connection = _TrackedConnection()
            tracked.append(connection)
            return connection

        raw_repository = self.RawNewsRepository(connect=tracked_connect)
        raw_claim = raw_repository.claim_next(
            timeout_minutes=30,
            lookahead_minutes=None,
            fresh_start_after=None,
        )
        self.assertIsNotNone(raw_claim)
        self.assertTrue(tracked[-1].closed)
        observer = _connect()
        cursor = observer.cursor()
        try:
            observer.start_transaction()
            cursor.execute("SELECT id FROM cryptonewsapi WHERE id=1 FOR UPDATE")
            self.assertEqual(cursor.fetchone(), (1,))
            time.sleep(0.05)  # Synthetic expensive work occurs outside the claim transaction.
            observer.rollback()
        finally:
            cursor.close()
            observer.close()
        assert raw_claim is not None
        self.assertTrue(raw_repository.complete(1, raw_claim.token))

        publication_repository = self.PublicationRepository(connect=tracked_connect)
        publication_claim = publication_repository.claim_next(
            timeout_minutes=30,
            fresh_start_after=None,
        )
        self.assertIsNotNone(publication_claim)
        self.assertTrue(tracked[-1].closed)
        observer = _connect()
        cursor = observer.cursor()
        try:
            observer.start_transaction()
            cursor.execute("SELECT id FROM rich_crpytonews WHERE id=101 FOR UPDATE")
            self.assertEqual(cursor.fetchone(), (101,))
            time.sleep(0.05)
            observer.rollback()
        finally:
            cursor.close()
            observer.close()
        assert publication_claim is not None
        self.assertTrue(publication_repository.complete(101, publication_claim.token))


if __name__ == "__main__":
    unittest.main(verbosity=2)
