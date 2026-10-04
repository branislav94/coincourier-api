from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import gpt_processor


def response_with_text(text="- Fresh verified fact.", annotations=None):
    return SimpleNamespace(
        status="completed", error=None, incomplete_details=None,
        output=[
            SimpleNamespace(type="web_search_call", status="completed"),
            SimpleNamespace(
                type="message", role="assistant", status="completed",
                content=[SimpleNamespace(type="output_text", text=text, annotations=annotations or [])],
            ),
        ],
    )


class OpenAIEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.responses.create.return_value = response_with_text()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.constructor = self.stack.enter_context(patch.object(gpt_processor, "OpenAI", return_value=self.client))
        self.stack.enter_context(patch.object(gpt_processor, "OPENAI_API_KEY", "sk-enrichment-fake-only"))
        self.stack.enter_context(patch.object(gpt_processor, "USE_WEB_SEARCH", True))
        self.stack.enter_context(patch.object(
            gpt_processor, "get_db_connection", side_effect=AssertionError("unexpected database connection"),
        ))

    def test_completed_search_context_remains_a_string_and_closes_client(self):
        result = gpt_processor.call_openai_enrichment("research prompt")
        self.assertEqual(result, "- Fresh verified fact.")
        self.assertIsInstance(result, str)
        self.client.__exit__.assert_called_once()

    def test_configured_output_token_budget_is_forwarded(self):
        with patch.object(gpt_processor, "ENRICHMENT_MAX_OUTPUT_TOKENS", 4096):
            gpt_processor.call_openai_enrichment("research prompt")
        self.assertEqual(self.client.responses.create.call_args.kwargs["max_output_tokens"], 4096)

    def test_context_is_bounded_and_retains_up_to_three_distinct_safe_sources(self):
        urls = [
            "javascript:unsafe",
            "https://credential:secret@example.invalid/unsafe",
            "https://:password@example.invalid/empty-username",
            "https://example.invalid/newline\nunsafe",
            "https://official.example.invalid/statement",
            "https://official.example.invalid/statement",
            "https://regulator.example.invalid/notice",
            "https://exchange.example.invalid/announcement",
            "https://fourth.example.invalid/extra",
        ]
        annotations = [SimpleNamespace(type="url_citation", url=url) for url in urls]
        self.client.responses.create.return_value = response_with_text("f" * 10000, annotations)
        result = gpt_processor.call_openai_enrichment("research prompt")
        self.assertLessEqual(len(result), gpt_processor.ENRICHMENT_MAX_CONTEXT_CHARACTERS)
        self.assertEqual(result.count("https://official.example.invalid/statement"), 1)
        self.assertIn("https://regulator.example.invalid/notice", result)
        self.assertIn("https://exchange.example.invalid/announcement", result)
        self.assertNotIn("fourth.example.invalid", result)
        self.assertNotIn("credential", result)
        self.assertNotIn("password", result)
        self.assertNotIn("javascript", result)
        self.assertNotIn("newline", result)

    def test_incomplete_failed_or_missing_response_status_is_rejected(self):
        for status in ("incomplete", "failed", "in_progress", "queued", "cancelled", None):
            with self.subTest(status=status):
                response = response_with_text("unsafe-provider-content")
                response.status = status
                self.client.responses.create.return_value = response
                with self.assertRaises(gpt_processor.EnrichmentRequestError) as raised:
                    gpt_processor.call_openai_enrichment("research prompt")
                self.assertNotIn("unsafe-provider-content", str(raised.exception))

    def test_response_error_or_incomplete_details_is_rejected(self):
        for name in ("error", "incomplete_details"):
            with self.subTest(field=name):
                response = response_with_text()
                setattr(response, name, SimpleNamespace(message="unsafe-provider-content"))
                self.client.responses.create.return_value = response
                with self.assertRaises(gpt_processor.EnrichmentRequestError) as raised:
                    gpt_processor.call_openai_enrichment("research prompt")
                self.assertNotIn("unsafe-provider-content", str(raised.exception))

    def test_search_must_have_executed_and_completed(self):
        for status in ("absent", "failed", "in_progress", "searching", None):
            with self.subTest(status=status):
                response = response_with_text()
                if status == "absent":
                    response.output.pop(0)
                else:
                    response.output[0].status = status
                self.client.responses.create.return_value = response
                with self.assertRaisesRegex(gpt_processor.EnrichmentRequestError, "completed web search"):
                    gpt_processor.call_openai_enrichment("research prompt")

    def test_refusal_or_partial_assistant_message_is_rejected(self):
        for refusal in (False, True):
            with self.subTest(refusal=refusal):
                response = response_with_text()
                if refusal:
                    response.output[1].content = [SimpleNamespace(type="refusal", refusal="unsafe-content")]
                else:
                    response.output[1].status = "incomplete"
                self.client.responses.create.return_value = response
                with self.assertRaises(gpt_processor.EnrichmentRequestError) as raised:
                    gpt_processor.call_openai_enrichment("research prompt")
                self.assertNotIn("unsafe-content", str(raised.exception))

    def test_empty_or_malformed_text_is_rejected(self):
        for text in ("", " \n ", None, {"unsafe": "response-body"}):
            with self.subTest(text=text):
                self.client.responses.create.return_value = response_with_text(text)
                with self.assertRaises(gpt_processor.EnrichmentRequestError):
                    gpt_processor.call_openai_enrichment("research prompt")
        self.client.responses.create.return_value = SimpleNamespace(status="completed", output={})
        with self.assertRaises(gpt_processor.EnrichmentRequestError):
            gpt_processor.call_openai_enrichment("research prompt")

    def test_retry_exhaustion_is_bounded_and_does_not_change_to_a_writer_call(self):
        failure = RuntimeError("sk-enrichment-fake-only unsafe-response")
        failure.status_code = 429
        self.client.responses.create.side_effect = failure
        with (
            patch.object(gpt_processor.time, "sleep") as sleep,
            patch.object(gpt_processor.random, "random", return_value=0),
            self.assertLogs(level="WARNING") as captured,
            self.assertRaisesRegex(gpt_processor.EnrichmentRequestError, "repeatedly failed"),
        ):
            gpt_processor.call_openai_enrichment("research prompt")
        self.assertEqual(self.client.responses.create.call_count, gpt_processor.MAX_RETRIES)
        self.assertEqual(sleep.call_count, gpt_processor.MAX_RETRIES)
        self.assertNotIn("unsafe-response", "\n".join(captured.output))
        self.client.chat.completions.create.assert_not_called()

    def test_search_disabled_preserves_empty_context_without_provider_call(self):
        with patch.object(gpt_processor, "USE_WEB_SEARCH", False):
            self.assertEqual(gpt_processor.enrich_with_search({"title": "story"}), "")
        self.constructor.assert_not_called()

    def test_research_prompt_keeps_source_body_bounded(self):
        self.assertEqual(gpt_processor.enrich_with_search({"title": "Crypto update", "text": "b" * 1500}),
                         "- Fresh verified fact.")
        prompt = self.client.responses.create.call_args.kwargs["input"]
        self.assertIn("Title: Crypto update", prompt)
        self.assertIn("official or primary sources", prompt)
        self.assertIn("Distinguish confirmed facts from uncertain claims", prompt)
        self.assertIn("Do not write the final article", prompt)
        self.assertIn("b" * 1200, prompt)
        self.assertNotIn("b" * 1201, prompt)

    def _mock_processing_consumers(self):
        document = {"full_text": "rewritten article", "schema_jsonld": "{}",
                    "seo_focus": "crypto", "seo_slug": "crypto-story", "seo_meta": "crypto update"}
        self.stack.enter_context(patch.object(gpt_processor, "_run_duplicate_shadow_fail_open"))
        self.stack.enter_context(patch.object(gpt_processor, "_run_semantic_shadow_fail_open"))
        self.stack.enter_context(patch.object(gpt_processor, "_maybe_video_url", return_value=""))
        writer = self.stack.enter_context(patch.object(gpt_processor, "classify_and_rewrite",
                                                      return_value=(document, "grok")))
        repair = self.stack.enter_context(patch.object(gpt_processor, "repair_if_needed", return_value=document))
        self.stack.enter_context(patch.object(gpt_processor, "validate_rewritten_article"))
        store = self.stack.enter_context(patch.object(gpt_processor, "store_rich_news"))
        mark = self.stack.enter_context(patch.object(gpt_processor, "mark_processed"))
        return writer, repair, store, mark

    def test_existing_writer_and_repair_receive_the_enrichment_string(self):
        writer, repair, store, mark = self._mock_processing_consumers()
        raw = {"id": 13, "title": "Crypto update", "news_url": "https://news.example.invalid/story"}
        self.assertTrue(gpt_processor.process_one(raw))
        writer.assert_called_once_with(raw, "- Fresh verified fact.", "")
        self.assertEqual(repair.call_args.args[1], "- Fresh verified fact.")
        self.assertEqual(repair.call_args.args[3], "grok")
        store.assert_called_once()
        mark.assert_called_once_with(raw["news_url"])

    def test_existing_writer_uses_grok_first_and_openai_only_after_primary_failure(self):
        document = {"title": "Verified crypto story"}
        for primary_fails, expected_provider in ((False, "grok"), (True, "openai")):
            with self.subTest(primary_fails=primary_fails):
                results = [RuntimeError("unit-test primary failure"), document] if primary_fails else [document]
                with (
                    patch.object(gpt_processor, "PRIMARY_LLM_PROVIDER", "grok"),
                    patch.object(gpt_processor, "LLM_FALLBACK_PROVIDER", "openai"),
                    patch.object(gpt_processor, "generate_json_with_provider", side_effect=results) as writer,
                ):
                    result = gpt_processor.generate_json_with_primary_provider(
                        "writer-model", phase="rewrite", messages=[{"role": "user", "content": "story"}],
                    )
                self.assertEqual(result, (document, expected_provider))
                self.assertEqual(
                    [call.args[0] for call in writer.call_args_list],
                    ["grok", "openai"] if primary_fails else ["grok"],
                )
        self.client.responses.create.assert_not_called()

    def test_enrichment_failure_keeps_claim_retryable_before_writer_or_persistence(self):
        writer, _repair, store, mark = self._mock_processing_consumers()
        raw = {"id": 13, "title": "Crypto update", "news_url": "https://news.example.invalid/story"}
        claim = SimpleNamespace(article=raw, token="claim-token-for-unit-test", attempt=1, recovered=False)
        repository = Mock()
        repository.claim_next.return_value = claim
        self.stack.enter_context(patch.object(gpt_processor, "RawNewsRepository", return_value=repository))
        self.client.responses.create.side_effect = RuntimeError("sk-enrichment-fake-only unsafe-response")
        with self.assertLogs(level="ERROR") as captured:
            result = gpt_processor._process_news_with_durable_claims(1, use_lookahead_filter=False)
        self.assertEqual(result, {"attempted": 1, "succeeded": 0, "failed": 1})
        repository.fail.assert_called_once()
        self.assertEqual(repository.fail.call_args.args[:2], (13, claim.token))
        self.assertIn("EnrichmentRequestError", repository.fail.call_args.args[2])
        self.assertNotIn("unsafe-response", repository.fail.call_args.args[2])
        self.assertNotIn("sk-enrichment-fake-only", "\n".join(captured.output))
        self.assertNotIn("unsafe-response", "\n".join(captured.output))
        repository.complete.assert_not_called()
        writer.assert_not_called()
        store.assert_not_called()
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
