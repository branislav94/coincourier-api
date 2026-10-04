"""Offline validation of the factual enrichment configuration and CLI profiles."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
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
from runtime.config_validation import format_config_issues, validate_runtime_config
import tasks


DEFAULTS = {
    "ENRICHMENT_MODEL": "gpt-5.6-luna",
    "ENRICHMENT_REASONING_EFFORT": "low",
    "ENRICHMENT_SEARCH_CONTEXT_SIZE": "low",
    "ENRICHMENT_MAX_OUTPUT_TOKENS": 1200,
}


def environment() -> dict[str, str]:
    values = dict(SAFE_TEST_ENVIRONMENT)
    for name in DEFAULTS:
        values.pop(name, None)
    values.update({
        "PUBLISH_API_TOKEN": "unit-test-only-publish-token",
        "PRIMARY_LLM_PROVIDER": "grok",
        "LLM_FALLBACK_PROVIDER": "openai",
        "PRIMARY_IMAGE_PROVIDER": "grok",
        "OPENAI_IMAGE_FALLBACK": "true",
    })
    return values


def load_config(values: dict[str, str]):
    spec = importlib.util.spec_from_file_location("enrichment_config_test", PROJECT_DIR / "config.py")
    if spec is None or spec.loader is None:
        raise AssertionError("Configuration module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, values, clear=True), patch.object(dotenv, "load_dotenv", return_value=False):
        spec.loader.exec_module(module)
    return module


class EnrichmentConfigurationTests(unittest.TestCase):
    def test_defaults_keep_writer_embedding_and_image_configuration_independent(self):
        config = load_config(environment())
        for name, expected in DEFAULTS.items():
            self.assertEqual(getattr(config, name), expected)
        self.assertEqual(config.PRIMARY_LLM_PROVIDER, "grok")
        self.assertEqual(config.LLM_FALLBACK_PROVIDER, "openai")
        self.assertEqual(config.OPENAI_TEXT_MODEL, "gpt-5")
        self.assertEqual(config.OPENAI_REASONING_EFFORT, "minimal")
        self.assertEqual(config.EMBEDDING_MODEL, "text-embedding-3-small")
        self.assertEqual(config.EMBEDDING_DIMENSIONS, 1536)
        self.assertFalse(config.EMBEDDING_ENABLED)
        self.assertEqual(config.GROK_IMAGE_MODEL, "grok-imagine-image-quality")
        self.assertEqual(config.OPENAI_IMAGE_MODEL, "gpt-image-1")
        self.assertEqual(config.IMAGE_SEARCH_ENGINE, "v1")

    def test_explicit_enrichment_settings_do_not_change_writer_defaults(self):
        values = environment()
        values.update({
            "ENRICHMENT_MODEL": " gpt-5.6-luna ",
            "ENRICHMENT_REASONING_EFFORT": " LOW ",
            "ENRICHMENT_SEARCH_CONTEXT_SIZE": " HIGH ",
            "ENRICHMENT_MAX_OUTPUT_TOKENS": "2048",
        })
        config = load_config(values)
        self.assertEqual(config.ENRICHMENT_MODEL, "gpt-5.6-luna")
        self.assertEqual(config.ENRICHMENT_REASONING_EFFORT, "low")
        self.assertEqual(config.ENRICHMENT_SEARCH_CONTEXT_SIZE, "high")
        self.assertEqual(config.ENRICHMENT_MAX_OUTPUT_TOKENS, 2048)
        self.assertEqual(config.OPENAI_MAX_OUTPUT_TOKENS, 4096)
        self.assertEqual(validate_runtime_config(values, profile="process"), ())

    def test_output_token_bounds_match_runtime_parsing_and_profile_validation(self):
        for value in ("1", "1200", "4096"):
            with self.subTest(value=value):
                values = environment()
                values["ENRICHMENT_MAX_OUTPUT_TOKENS"] = value
                self.assertEqual(load_config(values).ENRICHMENT_MAX_OUTPUT_TOKENS, int(value))
                self.assertEqual(validate_runtime_config(values, profile="process"), ())

    def test_invalid_settings_fail_parsing_and_validate_without_echoing_values(self):
        cases = (
            ("ENRICHMENT_MODEL", ""),
            ("ENRICHMENT_MODEL", "  "),
            ("ENRICHMENT_REASONING_EFFORT", ""),
            ("ENRICHMENT_REASONING_EFFORT", "high"),
            ("ENRICHMENT_SEARCH_CONTEXT_SIZE", ""),
            ("ENRICHMENT_SEARCH_CONTEXT_SIZE", "invalid-context-secret"),
            ("ENRICHMENT_MAX_OUTPUT_TOKENS", ""),
            ("ENRICHMENT_MAX_OUTPUT_TOKENS", "0"),
            ("ENRICHMENT_MAX_OUTPUT_TOKENS", "-1"),
            ("ENRICHMENT_MAX_OUTPUT_TOKENS", "4097"),
            ("ENRICHMENT_MAX_OUTPUT_TOKENS", "invalid-token-bound-secret"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                values = environment()
                values[name] = value
                with self.assertRaisesRegex(ValueError, name) as error:
                    load_config(values)
                for profile in ("process", "pipeline"):
                    issues = validate_runtime_config(values, profile=profile)
                    self.assertTrue(any(issue.field == name for issue in issues))
                    if value.endswith("secret"):
                        self.assertNotIn(value, str(error.exception))
                        self.assertNotIn(value, format_config_issues(issues))

    def test_all_supported_search_context_sizes_validate(self):
        for size in ("low", "medium", "high"):
            with self.subTest(size=size):
                values = environment()
                values["ENRICHMENT_SEARCH_CONTEXT_SIZE"] = size
                self.assertEqual(load_config(values).ENRICHMENT_SEARCH_CONTEXT_SIZE, size)
                self.assertEqual(validate_runtime_config(values, profile="process"), ())

    def test_processing_profiles_require_enrichment_key_even_with_only_grok_writer(self):
        values = environment()
        values.pop("OPENAI_API_KEY")
        values["LLM_FALLBACK_PROVIDER"] = "grok"
        values["OPENAI_IMAGE_FALLBACK"] = "false"
        for profile in ("process", "pipeline"):
            with self.subTest(profile=profile):
                rendered = format_config_issues(validate_runtime_config(values, profile=profile))
                self.assertIn("OPENAI_API_KEY: is required for OpenAI web-search enrichment", rendered)
                self.assertNotIn("OpenAI routing", rendered)

    def test_scheduler_web_profile_requires_enrichment_when_processing_is_active(self):
        values = environment()
        values["ENABLE_APSCHEDULER"] = "true"
        values.pop("OPENAI_API_KEY")
        rendered = format_config_issues(validate_runtime_config(values, profile="web"))
        self.assertIn("is required for OpenAI web-search enrichment", rendered)

    def test_disabled_web_profile_does_not_require_enrichment_credentials(self):
        values = environment()
        values.pop("OPENAI_API_KEY")
        values.pop("GROK_API_KEY")
        self.assertEqual(validate_runtime_config(values, profile="web"), ())

    def test_process_and_pipeline_config_check_pass_with_current_provider_keys_only(self):
        for profile in ("process", "pipeline"):
            with self.subTest(profile=profile):
                output = io.StringIO()
                with (
                    patch.dict(os.environ, environment(), clear=True),
                    patch.object(sys, "argv", ["tasks.py", "config_check", profile]),
                    patch("mysql.connector.connect") as connect,
                    patch("socket.create_connection") as create_connection,
                    patch("openai.OpenAI") as provider,
                    redirect_stdout(output),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(tasks.main(), 0)
                self.assertIn(f"Configuration valid for profile: {profile}", output.getvalue())
                connect.assert_not_called()
                create_connection.assert_not_called()
                provider.assert_not_called()

    def test_missing_enrichment_key_causes_config_check_failure_without_external_io(self):
        values = environment()
        values.pop("OPENAI_API_KEY")
        errors = io.StringIO()
        with (
            patch.dict(os.environ, values, clear=True),
            patch.object(sys, "argv", ["tasks.py", "config_check", "process"]),
            patch("mysql.connector.connect") as connect,
            patch("socket.create_connection") as create_connection,
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
        ):
            self.assertEqual(tasks.main(), 1)
        self.assertIn("OPENAI_API_KEY: is required for OpenAI web-search enrichment", errors.getvalue())
        self.assertNotIn(values["GROK_API_KEY"], errors.getvalue())
        connect.assert_not_called()
        create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
