"""Mocked shadow hook contracts; no live provider, DB or WordPress calls."""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from GetNewsAPI.tests.environment_isolation import install_test_environment

install_test_environment()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gpt_processor as gp
from publishing.models import PublicationArticle, PublicationContext, PublicationResult
from publishing.wordpress import publisher
from GetNewsAPI.tests.test_article_quality_v11 import article_document, SOURCE_URL
from GetNewsAPI.tests.test_publishing import ARTICLE_ROW, PUBLISHED_AT, response


class PublicCopyProcessingHookTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(gp, "get_db_connection", side_effect=AssertionError("unexpected DB")))
        self.stack.enter_context(patch.object(gp, "OpenAI", side_effect=AssertionError("unexpected provider")))
        self.stack.enter_context(patch.object(gp, "DUPLICATE_ENFORCEMENT_MODE", "off"))
        self.stack.enter_context(patch.object(gp, "_run_semantic_shadow_fail_open"))
        self.stack.enter_context(patch.object(gp, "_maybe_video_url", return_value=""))
        self.stack.enter_context(patch.object(gp, "PUBLIC_COPY_GATE_MODE", "shadow"))
        self.stack.enter_context(patch.dict(os.environ, {gp.ENABLE_ENV: "false"}))

    def test_shadow_finding_preserves_body_provider_calls_and_storage_then_completion(self):
        raw = {"id": 9001, "title": "Synthetic source", "news_url": SOURCE_URL}
        doc = article_document()
        doc["full_text"] += '<p>The supplied evidence in this prompt omits a later filing.</p>'
        before = deepcopy(doc)
        events = []
        with (
            patch.object(gp, "enrich_with_search", return_value="") as research,
            patch.object(gp, "_call_text_provider", return_value=json.dumps(doc)) as provider,
            patch.object(gp, "observe_public_copy", wraps=gp.observe_public_copy) as observer,
            patch.object(gp, "store_rich_news", side_effect=lambda *_: events.append("store")) as store,
            patch.object(gp, "mark_processed", side_effect=lambda *_: events.append("complete")) as complete,
        ):
            self.assertTrue(gp.process_one(raw))
        research.assert_called_once_with(raw)
        provider.assert_called_once()
        self.assertEqual(provider.call_args.kwargs["phase"], "rewrite")
        observer.assert_called_once_with(
            before["full_text"], mode="shadow", stage="pre_storage", raw_article_id=9001,
        )
        self.assertEqual(store.call_args.args, (before, raw))
        complete.assert_called_once_with(SOURCE_URL)
        self.assertEqual(events, ["store", "complete"])
        self.assertEqual(doc, before)

    def test_observer_receives_final_approved_href_cleanup(self):
        raw = {"id": 9002, "title": "Synthetic source", "news_url": SOURCE_URL + "?utm_source=feed"}
        doc = article_document()
        doc["full_text"] = doc["full_text"].replace(SOURCE_URL, raw["news_url"])
        with (
            patch.object(gp, "enrich_with_search", return_value=""),
            patch.object(gp, "classify_and_rewrite", return_value=(doc, "grok")),
            patch.object(gp, "repair_if_needed", return_value=doc),
            patch.object(gp, "observe_public_copy") as observer,
            patch.object(gp, "store_rich_news") as store,
        ):
            self.assertTrue(gp.process_one(raw, mark_complete=False))
        self.assertNotIn("utm_source", observer.call_args.args[0])
        self.assertEqual(observer.call_args.args[0], store.call_args.args[0]["full_text"])

    def test_unexpected_hook_exception_does_not_turn_success_into_retry(self):
        raw = {"id": 9003, "title": "Synthetic source", "news_url": SOURCE_URL}
        doc = article_document()
        with (
            patch.object(gp, "enrich_with_search", return_value=""),
            patch.object(gp, "classify_and_rewrite", return_value=(doc, "grok")) as writer,
            patch.object(gp, "repair_if_needed", return_value=doc) as repair,
            patch.object(gp, "observe_public_copy", side_effect=ValueError("PRIVATE_ERROR_CANARY")),
            patch.object(gp, "store_rich_news") as store,
        ):
            self.assertTrue(gp.process_one(raw, mark_complete=False))
        writer.assert_called_once()
        repair.assert_called_once()
        store.assert_called_once_with(doc, raw)

    def test_hard_invalid_output_does_not_reach_shadow_or_storage(self):
        raw = {"id": 9004, "title": "Synthetic source", "news_url": SOURCE_URL}
        doc = article_document()
        doc["full_text"] = "<p>word " + "word " * 448 + "</p>"
        with (
            patch.object(gp, "enrich_with_search", return_value=""),
            patch.object(gp, "classify_and_rewrite", return_value=(doc, "grok")),
            patch.object(gp, "repair_if_needed", return_value=doc),
            patch.object(gp, "observe_public_copy") as observer,
            patch.object(gp, "store_rich_news") as store,
        ):
            self.assertFalse(gp.process_one(raw, mark_complete=False))
        observer.assert_not_called()
        store.assert_not_called()


class PublicCopyPublishingHookTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.adapter = publisher.WordPressPublisher()
        self.context = PublicationContext(
            published_at_utc=PUBLISHED_AT, creation_is_fresh=True,
            raw_article_id=22, rich_article_id=11,
        )
        self.article = replace(PublicationArticle.from_mapping(ARTICLE_ROW),
            html_content='<p>The supplied evidence in this prompt omits a later filing.</p>',
            schema_jsonld=None)
        self.stack.enter_context(patch.object(publisher, "PUBLIC_COPY_GATE_MODE", "shadow"))
        self.stack.enter_context(patch.object(publisher, "PUBLISH_DAILY_HARD_CAP_ENABLED", False))
        self.stack.enter_context(patch.object(publisher.mysql.connector, "connect", side_effect=AssertionError("unexpected DB")))
        self.stack.enter_context(patch.object(publisher, "ensure_category", return_value=21))
        self.stack.enter_context(patch.object(publisher, "ensure_term", return_value=31))
        self.stack.enter_context(patch.object(publisher, "write_yoast_metadata"))

    def test_new_create_shadow_and_off_have_identical_payload_and_one_create(self):
        payloads = []
        for mode in ("off", "shadow"):
            with (
                self.subTest(mode=mode),
                patch.object(publisher, "PUBLIC_COPY_GATE_MODE", mode),
                patch.object(publisher, "observe_public_copy", wraps=publisher.observe_public_copy) as observer,
                patch.object(publisher.session, "post", return_value=response(body={"id": 101})) as post,
                patch("builtins.print"),
            ):
                result = self.adapter.publish(self.article, None, self.context)
                self.assertTrue(result.success)
                post.assert_called_once()
                observer.assert_called_once_with(
                    self.article.html_content, mode=mode, stage="pre_create", raw_article_id=22, rich_article_id=11,
                )
                payloads.append(deepcopy(post.call_args.kwargs["json"]))
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[1]["content"], self.article.html_content)

    def test_existing_id_reconciliation_never_observes_or_creates_again(self):
        reconciled = PublicationResult(success=True, external_id=101, reconciled=True)
        with (
            patch.object(self.adapter, "reconcile", return_value=reconciled),
            patch.object(publisher, "observe_public_copy") as observer,
            patch.object(publisher.session, "post") as post,
        ):
            result = self.adapter.publish(self.article, None, replace(self.context, existing_external_id=101))
        self.assertIs(result, reconciled)
        observer.assert_not_called()
        post.assert_not_called()

    def test_unexpected_hook_exception_preserves_create_and_metadata_result(self):
        with (
            patch.object(publisher, "observe_public_copy", side_effect=ValueError("PRIVATE_ERROR_CANARY")),
            patch.object(publisher.session, "post", return_value=response(body={"id": 101})) as post,
            patch("builtins.print"),
        ):
            result = self.adapter.publish(self.article, None, self.context)
        self.assertTrue(result.success)
        self.assertEqual(result.external_id, 101)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
