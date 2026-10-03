"""Opt-in end-to-end migration tooling checks against disposable MariaDBs."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from deployment.models import MigrationTarget
from deployment.preflight import LEDGER_TABLE, migration_lock
from deployment.service import MigrationService


RUN_INTEGRATION = os.getenv("RUN_MIGRATION_TOOLING_INTEGRATION", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
APP_HOST = os.getenv("MIGRATION_APP_TEST_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("MIGRATION_APP_TEST_PORT", "13310"))
APP_DATABASE = os.getenv("MIGRATION_APP_TEST_DATABASE", "coincourier_migration_test")
APP_USER = os.getenv("MIGRATION_APP_TEST_USER", "migration_test")
APP_PASSWORD = os.getenv("MIGRATION_APP_TEST_PASSWORD", "migration_test_only")
VECTOR_HOST = os.getenv("MIGRATION_VECTOR_TEST_HOST", "127.0.0.1")
VECTOR_PORT = int(os.getenv("MIGRATION_VECTOR_TEST_PORT", "13311"))
VECTOR_DATABASE = os.getenv(
    "MIGRATION_VECTOR_TEST_DATABASE", "coincourier_vectors_migration_test"
)
VECTOR_USER = os.getenv("MIGRATION_VECTOR_TEST_USER", "vector_migration_test")
VECTOR_PASSWORD = os.getenv("MIGRATION_VECTOR_TEST_PASSWORD", "vector_migration_test_only")


def _assert_disposable() -> None:
    if APP_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("application migration integration host must be loopback")
    if VECTOR_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("vector migration integration host must be loopback")
    if not APP_DATABASE.endswith("_test") or not VECTOR_DATABASE.endswith("_test"):
        raise RuntimeError("migration integration databases must end in _test")


def _connect(target: MigrationTarget, *, autocommit: bool = False):
    _assert_disposable()
    if target == MigrationTarget.APP:
        settings = (APP_HOST, APP_PORT, APP_DATABASE, APP_USER, APP_PASSWORD)
    else:
        settings = (VECTOR_HOST, VECTOR_PORT, VECTOR_DATABASE, VECTOR_USER, VECTOR_PASSWORD)
    host, port, database, user, password = settings
    return mysql.connector.connect(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        autocommit=autocommit,
        connection_timeout=10,
        ssl_disabled=True,
    )


def _environment() -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "MIGRATION_TEST_MODE": "true",
        "MIGRATION_LOCK_TIMEOUT_SECONDS": "1",
        "DB_HOST": APP_HOST,
        "DB_PORT": str(APP_PORT),
        "DB_NAME": APP_DATABASE,
        "DB_USER": APP_USER,
        "DB_PASSWORD": APP_PASSWORD,
        "DB_SSL_ENABLED": "false",
        "VECTOR_DB_HOST": VECTOR_HOST,
        "VECTOR_DB_PORT": str(VECTOR_PORT),
        "VECTOR_DB_NAME": VECTOR_DATABASE,
        "VECTOR_DB_USER": VECTOR_USER,
        "VECTOR_DB_PASSWORD": VECTOR_PASSWORD,
        "VECTOR_DB_SSL_ENABLED": "false",
    }


def _execute(target: MigrationTarget, sql: str) -> None:
    connection = _connect(target, autocommit=True)
    cursor = connection.cursor()
    try:
        cursor.execute(sql, map_results=True)
        while cursor.nextset():
            pass
    finally:
        cursor.close()
        connection.close()


def _scalar(target: MigrationTarget, sql: str):
    connection = _connect(target)
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        return cursor.fetchone()[0]
    finally:
        cursor.close()
        connection.close()


@unittest.skipUnless(
    RUN_INTEGRATION,
    "set RUN_MIGRATION_TOOLING_INTEGRATION=true for disposable MariaDB tests",
)
class MigrationToolingMariaDBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _assert_disposable()

    def setUp(self):
        baseline = (REPOSITORY_DIR / "maintenance/testing/mariadb_phase2_baseline.sql").read_text(
            encoding="utf-8"
        )
        _execute(MigrationTarget.APP, f"DROP TABLE IF EXISTS {LEDGER_TABLE};\n{baseline}")
        _execute(
            MigrationTarget.VECTOR,
            f"""
            SET FOREIGN_KEY_CHECKS=0;
            DROP TABLE IF EXISTS semantic_shadow_assessments;
            DROP TABLE IF EXISTS embedding_jobs;
            DROP TABLE IF EXISTS vector_chunks;
            DROP TABLE IF EXISTS vector_documents;
            DROP TABLE IF EXISTS {LEDGER_TABLE};
            SET FOREIGN_KEY_CHECKS=1;
            """,
        )
        self.service = MigrationService(
            connection_factory=lambda target: _connect(target),
            environment=_environment(),
            root=REPOSITORY_DIR,
        )

    def test_application_plan_check_apply_verify_and_second_apply_noop(self):
        self.assertEqual(self.service.plan(MigrationTarget.APP).outcome, "PLAN_READY")
        self.assertEqual(self.service.check(MigrationTarget.APP).outcome, "CHECK_PASSED")
        self.assertEqual(
            _scalar(
                MigrationTarget.APP,
                f"""
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='{LEDGER_TABLE}'
                """,
            ),
            0,
        )
        self.assertEqual(
            _scalar(
                MigrationTarget.APP,
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='cryptonewsapi'
                  AND COLUMN_NAME='processing_status'
                """,
            ),
            0,
        )
        first = self.service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(first.outcome, "APPLIED_AND_VERIFIED", "\n".join(first.lines))
        self.assertEqual(self.service.verify(MigrationTarget.APP).outcome, "VERIFIED")
        second = self.service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(second.outcome, "NO_PENDING_MIGRATIONS", "\n".join(second.lines))
        self.assertEqual(
            _scalar(MigrationTarget.APP, f"SELECT COUNT(*) FROM {LEDGER_TABLE}"), 3
        )

    def test_application_identity_collision_blocks_before_mutation(self):
        _execute(
            MigrationTarget.APP,
            """
            ALTER TABLE cryptonewsapi DROP INDEX uq_cryptonewsapi_news_url;
            INSERT INTO cryptonewsapi (news_url, title) VALUES
              ('https://collision.example.test/story', 'A'),
              ('https://collision.example.test/story', 'B');
            """,
        )
        result = self.service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(result.outcome, "PREFLIGHT_FAILED", "\n".join(result.lines))
        self.assertEqual(
            _scalar(
                MigrationTarget.APP,
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='cryptonewsapi'
                  AND COLUMN_NAME='processing_status'
                """,
            ),
            0,
        )
        self.assertEqual(
            _scalar(
                MigrationTarget.APP,
                f"""
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='{LEDGER_TABLE}'
                """,
            ),
            0,
        )

    def test_schema_without_ledger_is_blocked_as_untracked(self):
        script = (REPOSITORY_DIR / "maintenance/migrations/002_phase2_durable_state.sql").read_text(
            encoding="utf-8"
        )
        _execute(MigrationTarget.APP, script)
        result = self.service.plan(MigrationTarget.APP)
        self.assertEqual(result.outcome, "PLAN_BLOCKED")
        self.assertTrue(any("untracked_applied" in line for line in result.lines))

    def test_vector_plan_check_apply_verify_and_second_apply_noop(self):
        self.assertEqual(self.service.plan(MigrationTarget.VECTOR).outcome, "PLAN_READY")
        self.assertEqual(self.service.check(MigrationTarget.VECTOR).outcome, "CHECK_PASSED")
        self.assertEqual(
            _scalar(
                MigrationTarget.VECTOR,
                f"""
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='{LEDGER_TABLE}'
                """,
            ),
            0,
        )
        first = self.service.apply(MigrationTarget.VECTOR, allow_disposable=True)
        self.assertEqual(first.outcome, "APPLIED_AND_VERIFIED", "\n".join(first.lines))
        self.assertEqual(self.service.verify(MigrationTarget.VECTOR).outcome, "VERIFIED")
        second = self.service.apply(MigrationTarget.VECTOR, allow_disposable=True)
        self.assertEqual(second.outcome, "NO_PENDING_MIGRATIONS", "\n".join(second.lines))
        self.assertEqual(
            _scalar(MigrationTarget.VECTOR, f"SELECT COUNT(*) FROM {LEDGER_TABLE}"), 3
        )

    def test_vector_missing_semantic_schema_fails_despite_ledger(self):
        first = self.service.apply(MigrationTarget.VECTOR, allow_disposable=True)
        self.assertTrue(first.success, "\n".join(first.lines))
        _execute(MigrationTarget.VECTOR, "DROP TABLE semantic_shadow_assessments")
        result = self.service.verify(MigrationTarget.VECTOR)
        self.assertEqual(result.outcome, "VERIFY_FAILED")
        self.assertTrue(any("semantic_shadow_assessments" in line for line in result.lines))

    def test_ledger_checksum_drift_blocks_apply(self):
        first = self.service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertTrue(first.success, "\n".join(first.lines))
        _execute(
            MigrationTarget.APP,
            f"UPDATE {LEDGER_TABLE} SET checksum=REPEAT('0', 64) WHERE migration_id='app-002'",
        )
        plan = self.service.plan(MigrationTarget.APP)
        apply = self.service.apply(MigrationTarget.APP, allow_disposable=True)
        self.assertEqual(plan.outcome, "PLAN_BLOCKED")
        self.assertEqual(apply.outcome, "APPLY_BLOCKED")
        self.assertTrue(any("checksum" in line and "drift" in line for line in plan.lines))

    def test_concurrent_apply_lock_timeout_blocks_all_mutation(self):
        blocker = _connect(MigrationTarget.APP)
        try:
            with migration_lock(blocker, MigrationTarget.APP, APP_DATABASE, 1):
                result = self.service.apply(MigrationTarget.APP, allow_disposable=True)
            self.assertEqual(result.outcome, "APPLY_FAILED")
            self.assertTrue(any("lock was not acquired" in line for line in result.lines))
            self.assertEqual(
                _scalar(
                    MigrationTarget.APP,
                    """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='cryptonewsapi'
                      AND COLUMN_NAME='processing_status'
                    """,
                ),
                0,
            )
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
