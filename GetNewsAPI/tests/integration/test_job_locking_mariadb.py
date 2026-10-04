"""Opt-in DB-native operational lock checks against a disposable local MariaDB."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from operations.locking import advisory_job_lock, job_lock_name
from operations import jobs
from operations.models import DatabaseTarget, JobStatus, WorkResult


RUN_INTEGRATION = os.getenv("RUN_JOB_LOCK_INTEGRATION", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
HOST = os.getenv("JOB_LOCK_TEST_HOST", "127.0.0.1")
PORT = int(os.getenv("JOB_LOCK_TEST_PORT", "13310"))
DATABASE = os.getenv("JOB_LOCK_TEST_DATABASE", "coincourier_job_test")
USER = os.getenv("JOB_LOCK_TEST_USER", "job_test")
PASSWORD = os.getenv("JOB_LOCK_TEST_PASSWORD", "job_test_only")


def _assert_disposable() -> None:
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("job lock integration host must be loopback")
    if not DATABASE.endswith("_test"):
        raise RuntimeError("job lock integration database must end in _test")


def _connect(_target: DatabaseTarget = DatabaseTarget.APP):
    _assert_disposable()
    return mysql.connector.connect(
        host=HOST,
        port=PORT,
        database=DATABASE,
        user=USER,
        password=PASSWORD,
        connection_timeout=5,
        ssl_disabled=True,
    )


def _environment() -> dict[str, str]:
    return {"DB_NAME": DATABASE}


def _process_environment() -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "DB_USER": USER,
        "DB_PASSWORD": PASSWORD,
        "DB_HOST": HOST,
        "DB_PORT": str(PORT),
        "DB_NAME": DATABASE,
        "DB_SSL_ENABLED": "false",
        "ENABLE_APSCHEDULER": "false",
        "JOB_LOCK_TIMEOUT_SECONDS": "1",
        "VECTOR_ENABLED": "false",
        "EMBEDDING_ENABLED": "false",
        "SEMANTIC_SHADOW_ENABLED": "false",
        "PRIMARY_LLM_PROVIDER": "grok",
        "LLM_FALLBACK_PROVIDER": "openai",
        "GROK_API_KEY": "local-test-only",
        "OPENAI_API_KEY": "local-test-only",
    }


@unittest.skipUnless(
    RUN_INTEGRATION,
    "set RUN_JOB_LOCK_INTEGRATION=true for disposable MariaDB tests",
)
class JobLockMariaDBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _assert_disposable()

    def test_conflict_group_contention_skip_release_and_reacquire(self):
        with advisory_job_lock(
            "pipeline-workflow",
            DatabaseTarget.APP,
            environment=_environment(),
            timeout_seconds=1,
            connection_factory=_connect,
        ) as first:
            self.assertTrue(first.acquired)
            executor = Mock(return_value=WorkResult(JobStatus.NO_WORK, "process"))
            with patch.dict(jobs.EXECUTORS, {"process": executor}):
                conflicting = jobs.run_job(
                    "process",
                    environment=_process_environment(),
                    connection_factory=_connect,
                )
            self.assertEqual(conflicting.status, JobStatus.SKIPPED_ALREADY_RUNNING)
            self.assertEqual(conflicting.exit_code, 0)
            executor.assert_not_called()
            with advisory_job_lock(
                "fetch",
                DatabaseTarget.APP,
                environment=_environment(),
                timeout_seconds=1,
                connection_factory=_connect,
            ) as independent:
                self.assertTrue(independent.acquired)

        with advisory_job_lock(
            "pipeline-workflow",
            DatabaseTarget.APP,
            environment=_environment(),
            timeout_seconds=1,
            connection_factory=_connect,
        ) as subsequent:
            self.assertTrue(subsequent.acquired)

    def test_connection_close_releases_lock_and_lock_holds_no_transaction(self):
        name = job_lock_name("pipeline-workflow", DatabaseTarget.APP, DATABASE)
        connection = _connect()
        connection.autocommit = True
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT GET_LOCK(%s, %s)", (name, 1))
            self.assertEqual(cursor.fetchone(), (1,))
            cursor.execute("SELECT @@in_transaction")
            self.assertEqual(cursor.fetchone(), (0,))
        finally:
            cursor.close()
            connection.close()

        with advisory_job_lock(
            "pipeline-workflow",
            DatabaseTarget.APP,
            environment=_environment(),
            timeout_seconds=1,
            connection_factory=_connect,
        ) as next_attempt:
            self.assertTrue(next_attempt.acquired)


if __name__ == "__main__":
    unittest.main(verbosity=2)
