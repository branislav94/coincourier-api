"""Bootstrap and regression coverage for deterministic verification isolation."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
if str(REPOSITORY_DIR) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIR))
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from GetNewsAPI.tests.environment_isolation import (
    SAFE_TEST_ENVIRONMENT,
    install_test_environment,
    isolated_subprocess_environment,
)


install_test_environment()

import dotenv
from operations import jobs
from operations.models import JobStatus, WorkResult
import tasks


class EnvironmentIsolationTests(unittest.TestCase):
    def test_dotenv_files_cannot_override_test_connectivity(self):
        with patch("builtins.open") as open_file:
            self.assertFalse(
                dotenv.load_dotenv(PROJECT_DIR / ".env", override=True)
            )
        open_file.assert_not_called()

        self.assertEqual(os.environ["DB_HOST"], SAFE_TEST_ENVIRONMENT["DB_HOST"])
        self.assertEqual(os.environ["DB_PORT"], SAFE_TEST_ENVIRONMENT["DB_PORT"])

    def test_publish_task_uses_only_synthetic_database_configuration(self):
        cursors = []
        for row in ((SAFE_TEST_ENVIRONMENT["DB_NAME"],), (1,), (1,)):
            cursor = Mock()
            cursor.fetchone.return_value = row
            cursors.append(cursor)
        connection = Mock()
        connection.cursor.side_effect = cursors
        executor = Mock(return_value=WorkResult(JobStatus.NO_WORK, "publish"))
        output = io.StringIO()

        with (
            patch("mysql.connector.connect", return_value=connection) as connect,
            patch.object(socket, "create_connection") as create_connection,
            patch.dict(jobs.EXECUTORS, {"publish": executor}),
            redirect_stdout(output),
        ):
            self.assertEqual(tasks.run_publish(), 0)

        connect.assert_called_once()
        settings = connect.call_args.kwargs
        self.assertEqual(settings["host"], SAFE_TEST_ENVIRONMENT["DB_HOST"])
        self.assertEqual(settings["port"], int(SAFE_TEST_ENVIRONMENT["DB_PORT"]))
        self.assertEqual(settings["database"], SAFE_TEST_ENVIRONMENT["DB_NAME"])
        create_connection.assert_not_called()
        executor.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "NO_WORK")
        self.assertNotIn("password", output.getvalue().lower())
        self.assertNotIn("host", output.getvalue().lower())

    def test_subprocess_environment_is_safe_and_allows_explicit_test_overrides(self):
        with patch.dict(os.environ, {"UNRELATED_PARENT_SECRET": "must-not-leak"}):
            environment = isolated_subprocess_environment(
                {"DB_NAME": "explicit_disposable_test"}
            )
        self.assertEqual(environment["DB_HOST"], "127.0.0.1")
        self.assertEqual(environment["DB_PORT"], "1")
        self.assertEqual(environment["DB_NAME"], "explicit_disposable_test")
        self.assertEqual(environment["RUN_GROK_TEXT_SMOKE"], "false")
        self.assertEqual(environment["RUN_GROK_IMAGE_SMOKE"], "false")
        self.assertEqual(environment["GETNEWSAPI_RUNTIME_ENV_FILE"], ".env.example")
        self.assertNotIn("UNRELATED_PARENT_SECRET", environment)


if __name__ == "__main__":
    unittest.main()
