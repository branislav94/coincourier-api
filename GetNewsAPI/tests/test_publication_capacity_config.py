"""Offline defaults and validation for the optional publication hard cap."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import dotenv

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from runtime.config_validation import format_config_issues, validate_runtime_config
from tests.environment_isolation import SAFE_TEST_ENVIRONMENT


def load_config(values):
    path = Path(__file__).resolve().parents[1] / "config.py"
    spec = importlib.util.spec_from_file_location("publication_capacity_config_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, values, clear=True), patch.object(dotenv, "load_dotenv", return_value=False):
        spec.loader.exec_module(module)
    return module


class PublicationCapacityConfigurationTests(unittest.TestCase):
    def environment(self):
        values = dict(SAFE_TEST_ENVIRONMENT)
        values["PUBLISH_API_TOKEN"] = "unit-test-only-publish-token"
        return values

    def test_default_is_disabled_with_thirty_slots(self):
        values = self.environment()
        values.pop("PUBLISH_DAILY_HARD_CAP_ENABLED", None)
        values.pop("PUBLISH_DAILY_HARD_CAP", None)
        config = load_config(values)
        self.assertFalse(config.PUBLISH_DAILY_HARD_CAP_ENABLED)
        self.assertEqual(config.PUBLISH_DAILY_HARD_CAP, 30)

    def test_enabled_does_not_require_durable_flags(self):
        values = self.environment()
        values["PUBLISH_DAILY_HARD_CAP_ENABLED"] = "true"
        config = load_config(values)
        self.assertTrue(config.PUBLISH_DAILY_HARD_CAP_ENABLED)
        self.assertFalse(config.PUBLISH_DURABLE_STATE_ENABLED)
        self.assertEqual(validate_runtime_config(values, profile="publish"), ())

    def test_invalid_limit_rejected_even_while_disabled(self):
        for invalid in ("", "0", "-1", "1.5", "4294967296", "invalid-cap-secret"):
            with self.subTest(value=invalid):
                values = self.environment()
                values["PUBLISH_DAILY_HARD_CAP"] = invalid
                with self.assertRaisesRegex(ValueError, "PUBLISH_DAILY_HARD_CAP") as error:
                    load_config(values)
                issues = validate_runtime_config(values, profile="publish")
                self.assertTrue(any(issue.field == "PUBLISH_DAILY_HARD_CAP" for issue in issues))
                self.assertNotIn("invalid-cap-secret", str(error.exception))
                self.assertNotIn("invalid-cap-secret", format_config_issues(issues))

    def test_positive_integer_limits_are_accepted(self):
        for valid in ("1", "30", "4294967295"):
            with self.subTest(value=valid):
                values = self.environment()
                values["PUBLISH_DAILY_HARD_CAP"] = valid
                self.assertEqual(load_config(values).PUBLISH_DAILY_HARD_CAP, int(valid))
                self.assertEqual(validate_runtime_config(values, profile="publish"), ())

    def test_invalid_enabled_flag_is_not_silently_disabled(self):
        for invalid in ("", "  ", "invalid-flag-secret"):
            with self.subTest(value=invalid):
                values = self.environment()
                values["PUBLISH_DAILY_HARD_CAP_ENABLED"] = invalid
                with self.assertRaisesRegex(ValueError, "PUBLISH_DAILY_HARD_CAP_ENABLED") as error:
                    load_config(values)
                issues = validate_runtime_config(values, profile="publish")
                self.assertTrue(any(issue.field == "PUBLISH_DAILY_HARD_CAP_ENABLED" for issue in issues))
                self.assertNotIn("invalid-flag-secret", str(error.exception))
                self.assertNotIn("invalid-flag-secret", format_config_issues(issues))

    def test_offline_validation_does_not_access_database(self):
        values = self.environment()
        values["PUBLISH_DAILY_HARD_CAP_ENABLED"] = "true"
        with patch("socket.create_connection") as network:
            self.assertEqual(validate_runtime_config(values, profile="publish"), ())
        network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
