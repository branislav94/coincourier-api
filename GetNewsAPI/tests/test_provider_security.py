from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import gpt_processor
from operations import jobs
from operations.models import JobResult, JobStatus


GEMINI_TEST_SECRET = "super-secret-gemini-test-key"


class GeminiCredentialSafetyTests(unittest.TestCase):
    def test_gemini_uses_header_auth_and_a_keyless_url(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "grounded"}]}}]
        }
        with (
            patch.object(gpt_processor, "GOOGLE_API_KEY", GEMINI_TEST_SECRET),
            patch.object(
                gpt_processor.requests,
                "post",
                return_value=response,
            ) as post,
        ):
            self.assertEqual(gpt_processor.call_gemini_search("prompt"), "grounded")

        url = post.call_args.args[0]
        self.assertNotIn(GEMINI_TEST_SECRET, url)
        self.assertNotIn("?key=", url)
        self.assertEqual(
            post.call_args.kwargs["headers"],
            {"x-goog-api-key": GEMINI_TEST_SECRET},
        )
        self.assertEqual(post.call_args.kwargs["timeout"], 45)

    def test_transport_exception_logs_and_raises_without_secret(self):
        authenticated_url = (
            f"{gpt_processor.GEMINI_URL}?key={GEMINI_TEST_SECRET}"
        )
        failure = requests.ConnectionError(f"failed request to {authenticated_url}")
        with (
            patch.object(gpt_processor, "GOOGLE_API_KEY", GEMINI_TEST_SECRET),
            patch.object(gpt_processor.requests, "post", side_effect=failure) as post,
            self.assertLogs(level="ERROR") as captured,
            self.assertRaises(gpt_processor.GeminiRequestError) as raised,
        ):
            gpt_processor.call_gemini_search("prompt")

        post.assert_called_once()
        rendered_logs = "\n".join(captured.output)
        rendered_exception = str(raised.exception)
        self.assertNotIn(GEMINI_TEST_SECRET, rendered_logs)
        self.assertNotIn(GEMINI_TEST_SECRET, rendered_exception)
        self.assertIn("ConnectionError", rendered_logs)
        self.assertIn("ConnectionError", rendered_exception)

    def test_defensive_reason_redacts_google_query_and_header_credentials(self):
        error = RuntimeError(
            "request ?key=unexpected-google-secret "
            "x-goog-api-key: another-google-secret"
        )
        reason = gpt_processor._safe_ai_reason(error)
        self.assertNotIn("unexpected-google-secret", reason)
        self.assertNotIn("another-google-secret", reason)
        self.assertEqual(reason.count("[redacted]"), 2)

    def test_structured_task_output_contains_only_safe_error_type(self):
        failure = requests.ConnectionError(
            f"failed request to {gpt_processor.GEMINI_URL}?key={GEMINI_TEST_SECRET}"
        )
        work = jobs._failed("process", failure)
        now = datetime.now(timezone.utc)
        output = jobs.render_job_result(
            JobResult(
                job_name="process",
                status=JobStatus.FAILED,
                stage=work.stage,
                started_at=now,
                finished_at=now,
                reason_type=work.reason_type,
            )
        )
        self.assertNotIn(GEMINI_TEST_SECRET, output)
        self.assertIn('"reason_type":"ConnectionError"', output)

    def test_existing_provider_secret_redaction_is_preserved(self):
        with (
            patch.object(gpt_processor, "GROK_API_KEY", "xai-existing-secret"),
            patch.object(gpt_processor, "OPENAI_API_KEY", "sk-existing-secret"),
        ):
            reason = gpt_processor._safe_ai_reason(
                RuntimeError("xai-existing-secret sk-existing-secret")
            )
        self.assertNotIn("xai-existing-secret", reason)
        self.assertNotIn("sk-existing-secret", reason)


if __name__ == "__main__":
    unittest.main()
