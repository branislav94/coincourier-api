from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import traceback
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import gpt_processor
from operations import jobs
from operations.models import JobResult, JobStatus


ENRICHMENT_TEST_SECRET = "sk-enrichment-unit-test-secret"
UNSAFE_PROVIDER_CONTENT = "unsafe-provider-response-should-never-be-logged"


def completed_response():
    return {
        "id": "resp_unit_test",
        "object": "response",
        "created_at": 0,
        "model": "gpt-5.6-luna",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "id": "search_unit_test",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "official crypto update"},
            },
            {
                "id": "message_unit_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "grounded", "annotations": []}],
            },
        ],
    }


def sdk_client_factory(handler):
    """Exercise installed SDK serialization through an entirely local transport."""
    def factory(**kwargs):
        return OpenAI(
            **kwargs,
            base_url="https://api.openai.example.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    return factory


class OpenAIEnrichmentCredentialSafetyTests(unittest.TestCase):
    def assert_safe_failure(self, exception, logs):
        rendered = "\n".join(logs) + "\n" + "".join(
            traceback.format_exception(type(exception), exception, exception.__traceback__)
        )
        self.assertNotIn(ENRICHMENT_TEST_SECRET, rendered)
        self.assertNotIn(UNSAFE_PROVIDER_CONTENT, rendered)
        self.assertNotIn("Authorization: Bearer", rendered)

    def test_installed_sdk_uses_bearer_auth_and_keyless_responses_url(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completed_response())

        with (
            patch.object(gpt_processor, "OPENAI_API_KEY", ENRICHMENT_TEST_SECRET),
            patch.object(gpt_processor, "OpenAI", side_effect=sdk_client_factory(handler)) as client,
            patch.object(gpt_processor.requests, "post") as legacy_post,
        ):
            self.assertEqual(gpt_processor.call_openai_enrichment("prompt"), "grounded")

        client.assert_called_once_with(api_key=ENRICHMENT_TEST_SECRET, timeout=45, max_retries=0)
        legacy_post.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://api.openai.example.invalid/v1/responses")
        self.assertNotIn(ENRICHMENT_TEST_SECRET, str(requests[0].url))
        self.assertEqual(requests[0].headers["Authorization"], f"Bearer {ENRICHMENT_TEST_SECRET}")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload, {
            "model": "gpt-5.6-luna",
            "reasoning": {"effort": "low"},
            "tools": [{"type": "web_search", "search_context_size": "low"}],
            "tool_choice": "required",
            "store": False,
            "max_output_tokens": 1200,
            "input": "prompt",
        })
        self.assertNotIn(ENRICHMENT_TEST_SECRET, requests[0].content.decode())

    def test_transport_exception_and_traceback_do_not_expose_credentials(self):
        requests = []

        def handler(request):
            requests.append(request)
            raise httpx.ConnectError(
                f"Authorization: Bearer {ENRICHMENT_TEST_SECRET} {UNSAFE_PROVIDER_CONTENT}",
                request=request,
            )

        with (
            patch.object(gpt_processor, "OPENAI_API_KEY", ENRICHMENT_TEST_SECRET),
            patch.object(gpt_processor, "OpenAI", side_effect=sdk_client_factory(handler)),
            self.assertLogs(level="ERROR") as captured,
            self.assertRaises(gpt_processor.EnrichmentRequestError) as raised,
        ):
            gpt_processor.call_openai_enrichment("prompt")

        self.assertEqual(len(requests), 1)
        self.assertIn("APIConnectionError", str(raised.exception))
        self.assert_safe_failure(raised.exception, captured.output)

    def test_nonretryable_http_failure_suppresses_unsafe_response_body(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(401, json={"error": {"message": (
                f"Authorization: Bearer {ENRICHMENT_TEST_SECRET} {UNSAFE_PROVIDER_CONTENT}"
            )}})

        with (
            patch.object(gpt_processor, "OPENAI_API_KEY", ENRICHMENT_TEST_SECRET),
            patch.object(gpt_processor, "OpenAI", side_effect=sdk_client_factory(handler)),
            patch.object(gpt_processor.time, "sleep") as sleep,
            self.assertLogs(level="ERROR") as captured,
            self.assertRaises(gpt_processor.EnrichmentRequestError) as raised,
        ):
            gpt_processor.call_openai_enrichment("prompt")

        self.assertEqual(len(requests), 1)
        sleep.assert_not_called()
        self.assertIn("HTTP status 401", str(raised.exception))
        self.assert_safe_failure(raised.exception, captured.output)

    def test_retry_statuses_keep_existing_bounded_attempts_and_safe_logs(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) <= 2:
                return httpx.Response([429, 503][len(requests) - 1], json={
                    "error": {"message": f"{ENRICHMENT_TEST_SECRET} {UNSAFE_PROVIDER_CONTENT}"},
                })
            return httpx.Response(200, json=completed_response())

        with (
            patch.object(gpt_processor, "OPENAI_API_KEY", ENRICHMENT_TEST_SECRET),
            patch.object(gpt_processor, "OpenAI", side_effect=sdk_client_factory(handler)),
            patch.object(gpt_processor.time, "sleep") as sleep,
            patch.object(gpt_processor.random, "random", return_value=0.25),
            self.assertLogs(level="WARNING") as captured,
        ):
            self.assertEqual(gpt_processor.call_openai_enrichment("prompt"), "grounded")

        self.assertEqual(len(requests), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.25, 4.25])
        self.assertNotIn(ENRICHMENT_TEST_SECRET, "\n".join(captured.output))
        self.assertNotIn(UNSAFE_PROVIDER_CONTENT, "\n".join(captured.output))

    def test_client_construction_failure_is_safe(self):
        failure = RuntimeError(f"{ENRICHMENT_TEST_SECRET} {UNSAFE_PROVIDER_CONTENT}")
        with (
            patch.object(gpt_processor, "OPENAI_API_KEY", ENRICHMENT_TEST_SECRET),
            patch.object(gpt_processor, "OpenAI", side_effect=failure),
            self.assertLogs(level="ERROR") as captured,
            self.assertRaises(gpt_processor.EnrichmentRequestError) as raised,
        ):
            gpt_processor.call_openai_enrichment("prompt")
        self.assert_safe_failure(raised.exception, captured.output)

    def test_missing_key_fails_before_client_construction(self):
        with (
            patch.object(gpt_processor, "OPENAI_API_KEY", ""),
            patch.object(gpt_processor, "OpenAI") as client,
            self.assertRaises(gpt_processor.EnrichmentRequestError),
        ):
            gpt_processor.call_openai_enrichment("prompt")
        client.assert_not_called()

    def test_defensive_reason_redacts_query_and_header_credentials(self):
        reason = gpt_processor._safe_ai_reason(RuntimeError(
            "request ?key=unexpected-query-secret "
            "Authorization: Bearer unexpected-header-secret api-key: another-header-secret"
        ))
        for secret in ("unexpected-query-secret", "unexpected-header-secret", "another-header-secret"):
            self.assertNotIn(secret, reason)
        self.assertEqual(reason.count("[redacted]"), 3)

    def test_structured_task_output_contains_only_safe_error_type(self):
        failure = gpt_processor.EnrichmentRequestError(
            f"{ENRICHMENT_TEST_SECRET} {UNSAFE_PROVIDER_CONTENT}"
        )
        work = jobs._failed("process", failure)
        now = datetime.now(timezone.utc)
        output = jobs.render_job_result(JobResult(
            job_name="process", status=JobStatus.FAILED, stage=work.stage,
            started_at=now, finished_at=now, reason_type=work.reason_type,
        ))
        self.assertNotIn(ENRICHMENT_TEST_SECRET, output)
        self.assertNotIn(UNSAFE_PROVIDER_CONTENT, output)
        self.assertIn('"reason_type":"EnrichmentRequestError"', output)

    def test_existing_provider_secret_redaction_is_preserved(self):
        with (
            patch.object(gpt_processor, "GROK_API_KEY", "xai-existing-secret"),
            patch.object(gpt_processor, "OPENAI_API_KEY", "sk-existing-secret"),
        ):
            reason = gpt_processor._safe_ai_reason(RuntimeError("xai-existing-secret sk-existing-secret"))
        self.assertNotIn("xai-existing-secret", reason)
        self.assertNotIn("sk-existing-secret", reason)


if __name__ == "__main__":
    unittest.main()
