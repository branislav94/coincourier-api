from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import dotenv


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from tests.environment_isolation import SAFE_TEST_ENVIRONMENT

import tasks
from runtime.config_validation import PROFILES, format_config_issues, validate_runtime_config


CUTOFF_VARIABLE = "EMBEDDING_FRESH_START_AFTER_UTC"
VALID_TIMESTAMPS = (
    ("2026-10-04", datetime(2026, 10, 4)),
    ("2026-10-04 18:00:00", datetime(2026, 10, 4, 18)),
    ("2026-10-04T18:00:00Z", datetime(2026, 10, 4, 18)),
    ("2026-10-04T20:00:00+02:00", datetime(2026, 10, 4, 18)),
    ("2026-10-04T13:00:00-05:00", datetime(2026, 10, 4, 18)),
    (" 2026-10-04T18:00:00.123456Z ", datetime(2026, 10, 4, 18, microsecond=123456)),
    ("2026-10-04T20:00:00.123456+02:00", datetime(2026, 10, 4, 18, microsecond=123456)),
)
INVALID_TIMESTAMPS = (
    "invalid-cutoff-value",
    "2026-02-30T18:00:00Z",
    "2026-10-04T18:00:00+25:00",
    "0001-01-01T00:00:00+01:00",
)


def load_config(environment: dict[str, str]):
    spec = importlib.util.spec_from_file_location(
        "embedding_fresh_start_config_test",
        PROJECT_DIR / "config.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("Could not load configuration module")
    module = importlib.util.module_from_spec(spec)
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(dotenv, "load_dotenv", return_value=False) as load_dotenv,
    ):
        spec.loader.exec_module(module)
    load_dotenv.assert_called_once_with()
    return module


def web_environment() -> dict[str, str]:
    environment = dict(SAFE_TEST_ENVIRONMENT)
    environment.pop(CUTOFF_VARIABLE, None)
    environment.update(
        {
            "APP_ENV": "production",
            "PUBLISH_API_TOKEN": "unit-test-only-publish-token",
        }
    )
    return environment


class EmbeddingFreshStartParsingTests(unittest.TestCase):
    def test_unset_cutoff_preserves_no_filter_default(self):
        module = load_config(web_environment())
        self.assertIsNone(module.EMBEDDING_FRESH_START_AFTER_UTC)

    def test_empty_and_whitespace_cutoffs_preserve_no_filter_default(self):
        for value in ("", "  \t "):
            with self.subTest(value=value):
                environment = web_environment()
                environment[CUTOFF_VARIABLE] = value
                module = load_config(environment)
                self.assertIsNone(module.EMBEDDING_FRESH_START_AFTER_UTC)

    def test_valid_dates_and_offsets_normalize_to_naive_utc_without_losing_precision(self):
        for value, expected in VALID_TIMESTAMPS:
            with self.subTest(value=value):
                environment = web_environment()
                environment[CUTOFF_VARIABLE] = value
                module = load_config(environment)
                self.assertEqual(module.EMBEDDING_FRESH_START_AFTER_UTC, expected)
                self.assertIsNone(module.EMBEDDING_FRESH_START_AFTER_UTC.tzinfo)

    def test_malformed_or_unrepresentable_cutoffs_fail_import_with_name(self):
        for value in INVALID_TIMESTAMPS:
            with self.subTest(value=value):
                environment = web_environment()
                environment[CUTOFF_VARIABLE] = value
                with self.assertRaisesRegex(ValueError, CUTOFF_VARIABLE):
                    load_config(environment)

    def test_embedding_and_pipeline_cutoffs_are_independent(self):
        environment = web_environment()
        environment[CUTOFF_VARIABLE] = "2026-10-04T20:00:00.123456+02:00"
        environment["PIPELINE_FRESH_START_AFTER_UTC"] = "2026-06-27T20:00:00.654321+02:00"
        module = load_config(environment)
        self.assertEqual(
            module.EMBEDDING_FRESH_START_AFTER_UTC,
            datetime(2026, 10, 4, 18, microsecond=123456),
        )
        self.assertEqual(module.PIPELINE_FRESH_START_AFTER_UTC, datetime(2026, 6, 27, 18))
        self.assertEqual(module.PIPELINE_FRESH_START_AFTER_UTC_SQL, "2026-06-27 18:00:00")


class EmbeddingFreshStartValidationTests(unittest.TestCase):
    def test_unset_empty_and_whitespace_cutoffs_validate_with_features_off(self):
        for value in (None, "", "  \t "):
            with self.subTest(value=value):
                environment = web_environment()
                if value is not None:
                    environment[CUTOFF_VARIABLE] = value
                self.assertEqual(validate_runtime_config(environment, profile="web"), ())

    def test_valid_dates_and_offsets_validate_with_features_off(self):
        for value, _expected in VALID_TIMESTAMPS:
            with self.subTest(value=value):
                environment = web_environment()
                environment[CUTOFF_VARIABLE] = value
                self.assertEqual(validate_runtime_config(environment, profile="web"), ())

    def test_invalid_cutoffs_are_reported_for_every_supported_profile(self):
        for profile in sorted(PROFILES):
            for value in INVALID_TIMESTAMPS:
                with self.subTest(profile=profile, value=value):
                    environment = web_environment()
                    environment[CUTOFF_VARIABLE] = value
                    issues = validate_runtime_config(environment, profile=profile)
                    cutoff_issues = [issue for issue in issues if issue.field == CUTOFF_VARIABLE]
                    self.assertEqual(len(cutoff_issues), 1)
                    self.assertIn("must be a UTC timestamp", cutoff_issues[0].message)
                    self.assertNotIn(value, format_config_issues(issues))

    def test_config_check_returns_nonzero_without_external_io_for_invalid_cutoff(self):
        environment = web_environment()
        environment[CUTOFF_VARIABLE] = "invalid-cutoff-value"
        errors = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", ["tasks.py", "config_check", "web"]),
            patch("mysql.connector.connect") as connect,
            patch("socket.create_connection") as create_connection,
            redirect_stderr(errors),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(tasks.main(), 1)
        self.assertIn(f"{CUTOFF_VARIABLE}: must be a UTC timestamp", errors.getvalue())
        self.assertNotIn("invalid-cutoff-value", errors.getvalue())
        connect.assert_not_called()
        create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
