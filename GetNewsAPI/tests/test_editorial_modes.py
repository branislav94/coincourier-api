"""Mode configuration cannot silently ignore the observed production typo."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from runtime.config_validation import format_config_issues, validate_runtime_config
from runtime.editorial_modes import (
    EditorialModeError, duplicate_mode, editorial_mode_summary, public_copy_mode,
)
from tests.test_deployment import minimal_web_environment
from tests.environment_isolation import isolated_subprocess_environment
import tasks


class EditorialModeTests(unittest.TestCase):
    def test_defaults_are_shadow_without_enabling_cap_or_enforcement(self):
        self.assertEqual(editorial_mode_summary({}), {
            "duplicate_enforcement_mode": "shadow", "duplicate_mode_source": "default",
            "public_copy_gate_mode": "shadow", "public_copy_mode_source": "default",
        })

    def test_canonical_mode_has_one_parser_for_config_and_preflight(self):
        for raw, expected in (("off", "off"), ("shadow", "shadow"), (" ENFORCE ", "enforce")):
            with self.subTest(raw=raw):
                environment = minimal_web_environment()
                environment["DUPLICATE_ENFORCEMENT_MODE"] = raw
                self.assertEqual(duplicate_mode(environment).mode, expected)
                self.assertEqual(duplicate_mode(environment).source, "environment")
                self.assertEqual(validate_runtime_config(environment), ())

    def test_invalid_canonical_values_fail_without_rendering_the_value(self):
        for raw in ("emforce", "", "true", "semantic", "canary-secret-value", None):
            with self.subTest(raw=raw):
                environment = minimal_web_environment()
                environment["DUPLICATE_ENFORCEMENT_MODE"] = raw
                with self.assertRaises(EditorialModeError) as error:
                    duplicate_mode(environment)
                rendered = format_config_issues(validate_runtime_config(environment))
                self.assertIn("DUPLICATE_ENFORCEMENT_MODE: must be off, shadow or enforce", rendered)
                self.assertEqual("- " + str(error.exception), rendered)
                if raw:
                    self.assertNotIn(raw, rendered)

    def test_observed_misspelling_is_never_an_alias_or_silently_ignored(self):
        for raw in ("emforce", "enforce", "shadow", "", "canary-secret-value"):
            for canonical in (None, "shadow", "enforce"):
                with self.subTest(raw=raw, canonical=canonical):
                    environment = minimal_web_environment()
                    environment["DUPLICATE_ENFORCE_MODE"] = raw
                    if canonical is not None:
                        environment["DUPLICATE_ENFORCEMENT_MODE"] = canonical
                    with self.assertRaises(EditorialModeError):
                        duplicate_mode(environment)
                    rendered = format_config_issues(validate_runtime_config(environment))
                    self.assertIn("DUPLICATE_ENFORCE_MODE: is unsupported", rendered)
                    self.assertNotIn("canary-secret-value", rendered)

    def test_public_copy_is_advisory_only_and_explicit_blank_is_not_default(self):
        for raw in ("off", " SHADOW "):
            environment = {"PUBLIC_COPY_GATE_MODE": raw}
            self.assertEqual(public_copy_mode(environment).mode, raw.strip().lower())
        for raw in ("enforce", "", "true", "canary-secret-value"):
            with self.subTest(raw=raw):
                with self.assertRaises(EditorialModeError):
                    public_copy_mode({"PUBLIC_COPY_GATE_MODE": raw})

    def test_config_check_reports_only_validated_modes_and_their_source(self):
        environment = minimal_web_environment()
        environment["DUPLICATE_ENFORCEMENT_MODE"] = "shadow"
        environment["PUBLIC_COPY_GATE_MODE"] = "off"
        environment["UNRELATED_PRIVATE_VALUE"] = "canary-secret-value"
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
            self.assertTrue(tasks.run_config_check("web"))
        lines = output.getvalue().splitlines()
        summary = json.loads(lines[1].split(": ", 1)[1])
        self.assertEqual(summary, {
            "duplicate_enforcement_mode": "shadow", "duplicate_mode_source": "environment",
            "public_copy_gate_mode": "off", "public_copy_mode_source": "environment",
        })
        self.assertNotIn("canary-secret-value", output.getvalue())
        self.assertNotIn(environment["DB_PASSWORD"], output.getvalue())

    def test_loaded_config_rejects_typo_and_reports_canonical_default(self):
        # No dotenv reads or inherited credentials in the isolated child.
        script = (
            "import dotenv; dotenv.load_dotenv=lambda *a,**k:False; "
            "import config; print(config.DUPLICATE_ENFORCEMENT_MODE, "
            "config.DUPLICATE_ENFORCEMENT_MODE_SOURCE, config.PUBLIC_COPY_GATE_MODE)"
        )
        cwd = Path(__file__).resolve().parents[1]
        environment = isolated_subprocess_environment()
        environment.pop("DUPLICATE_ENFORCEMENT_MODE")
        valid = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=cwd, env=environment,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(valid.stdout.strip(), "shadow default shadow")
        for name in ("DUPLICATE_ENFORCE_MODE", "DUPLICATE_ENFORCEMENT_MODE"):
            with self.subTest(name=name):
                invalid = dict(environment, **{name: "canary-secret-value"})
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script], cwd=cwd, env=invalid,
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertNotIn("canary-secret-value", result.stderr)


if __name__ == "__main__":
    unittest.main()
