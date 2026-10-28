from __future__ import annotations

import base64
import importlib.util
import inspect
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCHEDULER_PATH = PROJECT_DIR / "scheduler.py"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import publish_to_wp
import tasks
from image_search.providers.openverse import OpenverseProvider
from publishing import PublicationArticle, PublicationContext, PublicationImage
from publishing.wordpress import client, media, publisher, seo, taxonomy


ARTICLE_ROW = {
    "id": 11,
    "durable_raw_article_id": 22,
    "title": "Bitcoin Markets Update",
    "full_text": "Markets liquidity improved on Coinbase.",
    "news_url": "https://news.example/article",
    "image_url": "https://images.example/hero.jpg",
    "category": "Markets, Analysis",
    "hashtags": "Bitcoin, ETF",
    "seo_slug": "bitcoin-markets-custom",
    "seo_focus": "Bitcoin market liquidity",
    "seo_meta": "A focused market summary.",
    "seo_canonical": "https://canonical.example/article",
    "published": 0,
    "publish_status": "fresh",
    "publish_attempt_count": 0,
    "publish_claim_token": None,
    "wp_post_id": None,
    "wp_post_url": None,
    "wp_post_created_at": None,
    "schema_jsonld": '{"@type":"NewsArticle"}',
}
PUBLISHED_AT = datetime(2026, 8, 31, 12, 34, 56, tzinfo=timezone.utc)


def context_connection(*, rows=None, first=None):
    connection = MagicMock()
    cursor = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    connection.cursor.return_value = cursor
    cursor.fetchall.return_value = [] if rows is None else rows
    cursor.fetchone.return_value = first
    return connection, cursor


def response(*, status=201, body=None, text=""):
    result = Mock()
    result.status_code = status
    result.reason = "reason"
    result.text = text
    result.json.return_value = {} if body is None else body
    return result


class CompatibilityTests(unittest.TestCase):
    def test_compatibility_import_remains_callable(self):
        self.assertTrue(callable(publish_to_wp.publish_news_to_wp))
        self.assertTrue(callable(publish_to_wp.slugify))

    def test_compatibility_module_delegates_to_extracted_publisher(self):
        self.assertIs(publish_to_wp, publisher)

        lock_conn = MagicMock()
        lock_conn.is_connected.return_value = True
        adapter = Mock()
        adapter.reconcile.return_value = None
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=lock_conn),
            patch.object(publisher, "_get_lock", return_value=True),
            patch.object(publisher, "_release_lock", return_value=True),
            patch.object(publisher, "_count_due_now", return_value=1),
            patch.object(publisher, "fetch_unpublished", return_value=[ARTICLE_ROW]),
            patch.object(publisher, "PublicationRepository", return_value=Mock(
                get_external_post_state=Mock(return_value=ARTICLE_ROW),
            )),
            patch.object(publisher, "upload_image", return_value=(None, {})),
            patch.object(publisher, "mark_news_as_published") as mark_published,
            patch.object(publisher, "WordPressPublisher", return_value=adapter) as adapter_type,
        ):
            adapter.publish.return_value = publisher.PublicationResult(
                success=True,
                external_id=101,
            )
            result = publish_to_wp.publish_news_to_wp()

        self.assertEqual(
            result,
            {"status": "success", "attempted": 1, "succeeded": 1, "failed": 0},
        )
        adapter_type.assert_called_once_with()
        published_article, published_image, context = adapter.publish.call_args.args
        self.assertEqual(published_article.title, ARTICLE_ROW["title"])
        self.assertIsNone(published_image)
        self.assertIsInstance(context, PublicationContext)
        mark_published.assert_called_once_with(ARTICLE_ROW["news_url"])

    def test_batch_does_not_mark_when_adapter_reports_failure(self):
        lock_conn = MagicMock()
        lock_conn.is_connected.return_value = True
        adapter = Mock()
        adapter.reconcile.return_value = None
        adapter.publish.return_value = publisher.PublicationResult(
            success=False,
            error="failed",
        )
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=lock_conn),
            patch.object(publisher, "_get_lock", return_value=True),
            patch.object(publisher, "_release_lock", return_value=True),
            patch.object(publisher, "_count_due_now", return_value=1),
            patch.object(publisher, "fetch_unpublished", return_value=[ARTICLE_ROW]),
            patch.object(publisher, "PublicationRepository", return_value=Mock(
                get_external_post_state=Mock(return_value=ARTICLE_ROW),
            )),
            patch.object(publisher, "upload_image", return_value=(None, {})),
            patch.object(publisher, "mark_news_as_published") as mark_published,
            patch.object(publisher, "WordPressPublisher", return_value=adapter),
        ):
            result = publish_to_wp.publish_news_to_wp()
        self.assertEqual(
            result,
            {"status": "failed", "attempted": 1, "succeeded": 0, "failed": 1},
        )
        mark_published.assert_not_called()

    def test_tasks_routes_publish_through_operational_job(self):
        with patch.object(tasks, "_run_operational_job", return_value=0) as run:
            self.assertEqual(tasks.run_publish(), 0)
        run.assert_called_once_with("publish")

    def test_scheduler_routes_chained_job_through_pipeline_contract(self):
        fake_fetcher = types.ModuleType("fetcher")
        fake_fetcher.start_scheduler = Mock()
        fake_fetcher.stop_scheduler = Mock()

        spec = importlib.util.spec_from_file_location("scheduler_contract_test", SCHEDULER_PATH)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        scheduler_module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                "fetcher": fake_fetcher,
            },
        ):
            spec.loader.exec_module(scheduler_module)

        result = Mock(exit_code=0)
        with (
            patch.object(scheduler_module, "run_job", return_value=result) as run,
            patch.object(scheduler_module, "render_job_result", return_value="{}"),
        ):
            self.assertIs(scheduler_module.chained_job(), result)
        run.assert_called_once_with("pipeline_once")

    def test_publication_article_maps_only_current_fields(self):
        article = PublicationArticle.from_mapping(ARTICLE_ROW)
        self.assertEqual(article.html_content, ARTICLE_ROW["full_text"])
        self.assertEqual(article.categories, ARTICLE_ROW["category"])
        self.assertEqual(article.tags, ARTICLE_ROW["hashtags"])

    def test_replacing_legacy_session_reaches_extracted_taxonomy(self):
        replacement = Mock()
        replacement.get.return_value = response(status=200, body=[{"id": 19}])
        with (
            patch.object(publisher, "session", replacement),
            patch.object(publisher, "WP_API_URL", "https://patched.example"),
        ):
            category_id = publisher.ensure_category("Markets")
        self.assertEqual(category_id, 19)
        replacement.get.assert_called_once_with(
            "https://patched.example/wp-json/wp/v2/categories",
            params={"slug": "markets"},
            timeout=client.WP_HTTP_TIMEOUT,
        )

    def test_new_media_owner_patch_controls_legacy_helper(self):
        with patch.object(media, "set_media_details") as details:
            publisher.set_media_details(20, "Alt", caption="Credit")
        details.assert_called_once_with(
            20,
            "Alt",
            caption="Credit",
            description=None,
            required_attribution=False,
            http_session=publisher.session,
            api_url=publisher.WP_API_URL,
        )


class WordPressClientTests(unittest.TestCase):
    def test_wordpress_authentication_headers_remain_equivalent(self):
        http_session = client.create_authenticated_session("writer", "secret")
        token = base64.b64encode(b"writer:secret").decode()
        self.assertEqual(http_session.headers["Authorization"], f"Basic {token}")
        self.assertEqual(
            http_session.headers["User-Agent"],
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124 Safari/537.36",
        )

    def test_media_upload_request_remains_equivalent(self):
        post = Mock(return_value=response(body={"id": 71}))
        media_id = media.upload_media(
            b"image-bytes",
            "hero.jpg",
            post_with_retries=post,
        )
        self.assertEqual(media_id, 71)
        post.assert_called_once_with(
            f"{client.API_BASE}/wp-json/wp/v2/media",
            headers={
                "Content-Disposition": "attachment; filename=hero.jpg",
                "Content-Type": "image/jpeg",
            },
            data=b"image-bytes",
            timeout=client.WP_HTTP_TIMEOUT,
        )

    def test_media_retry_behavior_preserves_supported_statuses(self):
        for status in sorted(client.RETRY_STATUS):
            with self.subTest(status=status):
                http_session = Mock()
                http_session.post.side_effect = [
                    response(status=status),
                    response(status=201, body={"id": 9}),
                ]
                with (
                    patch.object(client.time, "sleep") as sleep,
                    patch("builtins.print"),
                ):
                    result = client.post_with_retries(
                        http_session,
                        "https://wp.example/media",
                        pause_s=10,
                    )
                self.assertEqual(result.status_code, 201)
                self.assertEqual(http_session.post.call_count, 2)
                sleep.assert_called_once_with(10)

    def test_connect_timeout_can_retry_before_mutation_is_sent(self):
        http_session = Mock()
        http_session.post.side_effect = [
            requests.ConnectTimeout("connection not established"),
            response(status=201),
        ]
        with (
            patch.object(client.time, "sleep") as sleep,
            patch("builtins.print"),
        ):
            result = client.post_with_retries(http_session, "https://wp.example/media")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(http_session.post.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_read_timeout_is_not_retried_after_ambiguous_mutation(self):
        http_session = Mock()
        http_session.post.side_effect = [
            requests.ReadTimeout("response not received"),
            response(status=201),
        ]
        with (
            patch.object(client.time, "sleep") as sleep,
            patch("builtins.print"),
            self.assertRaises(requests.ReadTimeout),
        ):
            client.post_with_retries(http_session, "https://wp.example/media")
        self.assertEqual(http_session.post.call_count, 1)
        sleep.assert_not_called()

    def test_generic_request_exception_is_not_retried(self):
        http_session = Mock()
        http_session.post.side_effect = [
            requests.RequestException("ambiguous failure"),
            response(status=201),
        ]
        with (
            patch.object(client.time, "sleep") as sleep,
            patch("builtins.print"),
            self.assertRaises(requests.RequestException),
        ):
            client.post_with_retries(http_session, "https://wp.example/media")
        self.assertEqual(http_session.post.call_count, 1)
        sleep.assert_not_called()

    def test_media_retry_zero_attempts_preserves_implicit_none(self):
        http_session = Mock()
        self.assertIsNone(
            client.post_with_retries(
                http_session,
                "https://wp.example/media",
                max_tries=0,
            )
        )
        http_session.post.assert_not_called()

    def test_media_details_payload_preserves_limits(self):
        http_response = Mock()
        with patch.object(media.session, "post", return_value=http_response) as post:
            media.set_media_details(
                72,
                "a" * 130,
                caption="c" * 510,
                description="d" * 1010,
            )
        post.assert_called_once_with(
            f"{media.WP_API_URL}/wp-json/wp/v2/media/72",
            json={
                "alt_text": "a" * 120,
                "caption": "c" * 500,
                "description": "d" * 1000,
            },
            timeout=client.WP_HTTP_TIMEOUT,
        )
        http_response.raise_for_status.assert_called_once_with()

    def test_required_media_credit_preserves_complete_long_attribution(self):
        credit = "Required notice. " * 90 + "Creator; https://source.example; CC BY 4.0; https://creativecommons.org/licenses/by/4.0/"
        http = Mock()
        saved = media.set_media_details(
            72, "Alt", caption=credit, description=credit,
            required_attribution=True, http_session=http,
        )
        self.assertTrue(saved)
        self.assertGreater(len(credit), 1000)
        payload = http.post.call_args.kwargs["json"]
        self.assertEqual(payload["caption"], credit)
        self.assertEqual(payload["description"], credit)

    def test_required_media_metadata_failure_raises_sanitized_error(self):
        http = Mock()
        http.post.return_value.raise_for_status.side_effect = requests.HTTPError(
            "sensitive-token complete-request-headers"
        )
        with self.assertLogs(media.logger, level="WARNING") as logs:
            with self.assertRaises(media.MediaMetadataError) as caught:
                media.set_media_details(
                    72, "Alt", caption="Credit", description="Credit",
                    required_attribution=True, http_session=http,
                )
        self.assertEqual(caught.exception.reason, "metadata_update_failed")
        self.assertIn("media_id=72", " ".join(logs.output))
        self.assertNotIn("sensitive-token", " ".join(logs.output))
        self.assertNotIn("sensitive-token", str(caught.exception))

    def test_optional_media_metadata_failure_returns_false(self):
        http = Mock()
        http.post.side_effect = requests.ConnectionError("unavailable")
        with self.assertLogs(media.logger, level="WARNING"):
            saved = media.set_media_details(72, "Alt", http_session=http)
        self.assertFalse(saved)

    def test_invalid_required_credit_rejects_before_request_without_truncation(self):
        for credit, reason in (
            ("", "attribution_missing"),
            ("x" * (media.MAX_REQUIRED_ATTRIBUTION_CHARS + 1), "attribution_too_large"),
        ):
            with self.subTest(reason=reason):
                http = Mock()
                with self.assertLogs(media.logger, level="WARNING"):
                    with self.assertRaises(media.MediaMetadataError) as caught:
                        media.set_media_details(
                            72, "Alt", caption=credit, description=credit,
                            required_attribution=True, http_session=http,
                        )
                self.assertEqual(caught.exception.reason, reason)
                http.post.assert_not_called()


class TaxonomyTests(unittest.TestCase):
    def test_existing_category_resolution_remains_equivalent(self):
        found = response(status=200, body=[{"id": 11}])
        with (
            patch.object(taxonomy.session, "get", return_value=found) as get,
            patch.object(taxonomy.session, "post") as post,
        ):
            category_id = taxonomy.ensure_category("Market News")
        self.assertEqual(category_id, 11)
        get.assert_called_once_with(
            f"{taxonomy.WP_API_URL}/wp-json/wp/v2/categories",
            params={"slug": "market-news"},
            timeout=client.WP_HTTP_TIMEOUT,
        )
        post.assert_not_called()

    def test_category_creation_request_remains_equivalent(self):
        missing = response(status=200, body=[])
        created = response(status=201, body={"id": 12})
        with (
            patch.object(taxonomy.session, "get", return_value=missing),
            patch.object(taxonomy.session, "post", return_value=created) as post,
        ):
            category_id = taxonomy.ensure_category("Market News")
        self.assertEqual(category_id, 12)
        post.assert_called_once_with(
            f"{taxonomy.WP_API_URL}/wp-json/wp/v2/categories",
            json={"name": "Market News", "slug": "market-news"},
            timeout=client.WP_HTTP_TIMEOUT,
        )

    def test_tag_creation_request_remains_equivalent(self):
        missing = response(status=200, body=[])
        created = response(status=201, body={"id": 13})
        with (
            patch.object(taxonomy.session, "get", return_value=missing) as get,
            patch.object(taxonomy.session, "post", return_value=created) as post,
        ):
            tag_id = taxonomy.ensure_term("Bitcoin ETF", "tags")
        self.assertEqual(tag_id, 13)
        get.assert_called_once_with(
            f"{taxonomy.API_BASE}/wp-json/wp/v2/tags",
            params={"slug": "bitcoin-etf"},
            timeout=client.WP_HTTP_TIMEOUT,
        )
        post.assert_called_once_with(
            f"{taxonomy.API_BASE}/wp-json/wp/v2/tags",
            json={"name": "Bitcoin ETF", "slug": "bitcoin-etf"},
            timeout=client.WP_HTTP_TIMEOUT,
        )


class SeoAndDatabaseTests(unittest.TestCase):
    def test_yoast_metadata_native_rest_round_trip_preserves_full_fields(self) -> None:
        """Write supported metadata through WordPress and verify an authenticated read."""
        http = Mock()
        expected = {"_yoast_wpseo_focuskw": "focus", "_yoast_wpseo_metadesc": "d" * 170,
                    "_yoast_wpseo_title": "t" * 70}
        http.post.return_value = response(status=200, body={"id": 91, "meta": expected})
        http.get.return_value = response(status=200, body={"id": 91, "meta": expected})
        with patch.object(seo, "get_wp_prefix") as prefix:
            seo.write_yoast_metadata(http, 91, focus_keyword="focus", description="d" * 170,
                                     title="t" * 70, canonical_url=None,
                                     api_base="https://wp.example", timeout=(1, 2))
        prefix.assert_not_called()
        http.post.assert_called_once_with("https://wp.example/wp-json/wp/v2/posts/91",
                                          json={"meta": expected}, timeout=(1, 2))
        self.assertEqual(http.get.call_args.args[0], "https://wp.example/wp-json/wp/v2/posts/91")
        self.assertEqual(http.get.call_args.kwargs["params"]["context"], "edit")
        self.assertEqual(http.get.call_args.kwargs["timeout"], (1, 2))

    def test_custom_canonical_remains_outside_three_field_rest_contract(self) -> None:
        """Leave canonical untouched while successfully synchronizing the supported fields."""
        http = Mock()
        fields = {
            "_yoast_wpseo_focuskw": "focus", "_yoast_wpseo_title": "title",
            "_yoast_wpseo_metadesc": "description",
        }
        http.post.return_value = response(status=200, body={"id": 92})
        http.get.return_value = response(status=200, body={
            "id": 92, "meta": {**fields, "_yoast_wpseo_canonical": "https://wp.example/existing"},
        })
        with (
            patch.object(seo, "get_wp_prefix") as prefix,
            self.assertLogs(seo.logger, level="WARNING") as captured,
        ):
            seo.write_yoast_metadata(http, 92, focus_keyword="focus", description="description",
                                     title="title", canonical_url="https://canonical.example/post",
                                     api_base="https://wp.example", timeout=(1, 2))
        self.assertEqual(http.post.call_args.kwargs["json"], {"meta": fields})
        self.assertEqual(http.get.call_count, 1)
        self.assertIn("unsupported-rest-field", "\n".join(captured.output))
        prefix.assert_not_called()

    def test_missing_returned_metadata_fails_even_with_successful_http(self) -> None:
        """Missing protected metadata cannot be accepted as synchronization success."""
        http = Mock()
        http.post.return_value = response(status=200, body={"id": 93})
        http.get.return_value = response(status=200, body={"id": 93})
        with self.assertRaises(seo.MetadataSyncError):
            seo.write_yoast_metadata(http, 93, focus_keyword="focus", description="description",
                                     title="title", canonical_url=None,
                                     api_base="https://wp.example", timeout=(1, 2))

    def test_application_published_state_sql_remains_equivalent(self):
        connection, cursor = context_connection()
        with patch.object(
            publisher.mysql.connector,
            "connect",
            return_value=connection,
        ):
            publisher.mark_news_as_published("https://news.example/article")
        cursor.execute.assert_called_once_with(
            "UPDATE rich_crpytonews SET published = 1 WHERE news_url = %s",
            ("https://news.example/article",),
        )
        connection.commit.assert_called_once_with()

    def test_fetch_unpublished_sql_preserves_due_filters_and_limit(self):
        connection, cursor = context_connection(rows=[ARTICLE_ROW])
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=connection),
            patch.object(publisher, "PIPELINE_FRESH_START_AFTER_UTC_SQL", ""),
        ):
            rows = publisher.fetch_unpublished(limit=7)
        sql, params = cursor.execute.call_args.args
        self.assertEqual(rows, [ARTICLE_ROW])
        self.assertIn("r.published = 0", sql)
        self.assertIn("manual_review", sql)
        self.assertIn("c.chosen_for_publish = 1", sql)
        self.assertIn("c.scheduled_for <= UTC_TIMESTAMP()", sql)
        self.assertIn("ORDER BY c.scheduled_for ASC", sql)
        self.assertEqual(params, (7,))


    def test_due_count_excludes_manual_reconciliation_rows(self):
        connection, cursor = context_connection(first=(2,))
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=connection),
            patch.object(publisher, "PIPELINE_FRESH_START_AFTER_UTC_SQL", ""),
        ):
            self.assertEqual(publisher._count_due_now(), 2)
        sql, params = cursor.execute.call_args.args
        self.assertIn("manual_review", sql)
        self.assertIn("r.published = 0", sql)
        self.assertEqual(params, ())

class WordPressPublisherTests(unittest.TestCase):
    def setUp(self):
        self.article = PublicationArticle.from_mapping(ARTICLE_ROW)
        self.context = PublicationContext(published_at_utc=PUBLISHED_AT, creation_is_fresh=True)

    def publish_with_mocks(self, *, image=None, post_response=None, events=None):
        post_response = post_response or response(body={"id": 101})
        wp_connection = MagicMock()
        wp_connection.__enter__.return_value = wp_connection
        wp_connection.__exit__.return_value = False
        event_log = [] if events is None else events
        patches = [
            patch.object(publisher, "ensure_category", side_effect=[21, 22]),
            patch.object(publisher, "ensure_term", side_effect=[31, 32]),
            patch.object(publisher.session, "post", return_value=post_response),
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection),
            patch.object(
                publisher,
                "record_image_usage",
                side_effect=lambda *_args: event_log.append("usage"),
            ),
            patch.object(
                publisher,
                "write_yoast_metadata",
                side_effect=lambda *_args, **_kwargs: event_log.append("yoast"),
            ),
        ]
        mocks = [item.start() for item in patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
        with patch("builtins.print"):
            result = publisher.WordPressPublisher().publish(
                self.article,
                image,
                self.context,
            )
        return result, mocks, wp_connection, event_log

    def test_wordpress_post_payload_remains_equivalent(self):
        image = PublicationImage(external_id=77, metadata={"provider": "openverse"})
        result, mocks, _connection, _events = self.publish_with_mocks(image=image)
        post = mocks[2]
        self.assertTrue(result.success)
        self.assertEqual(result.external_id, 101)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "title": "Bitcoin Markets Update",
                "content": (
                    '<a href="/category/markets/">Markets</a> liquidity improved on '
                    '<a href="https://www.coinbase.com" target="_blank" '
                    'rel="noopener">Coinbase</a>.\n'
                    '<script type="application/ld+json">'
                    '{"@type":"NewsArticle"}</script>\n'
                ),
                "status": "publish",
                "slug": "bitcoin-markets-custom",
                "date_gmt": "2026-08-31T12:34:56",
                "categories": [21, 22],
                "tags": [31, 32],
                "featured_media": 77,
            },
        )
        self.assertEqual(
            post.call_args.args[0],
            f"{publisher.WP_API_URL}/wp-json/wp/v2/posts",
        )
        self.assertEqual(
            post.call_args.kwargs["timeout"],
            client.WP_HTTP_TIMEOUT,
        )

    def test_post_read_timeout_is_not_immediately_retried(self):
        adapter = publisher.WordPressPublisher()
        with (
            patch.object(adapter, "reconcile", return_value=None),
            patch.object(publisher, "ensure_category", return_value=21),
            patch.object(publisher, "ensure_term", return_value=31),
            patch.object(
                publisher.session,
                "post",
                side_effect=requests.ReadTimeout("ambiguous post outcome"),
            ) as post,
            self.assertRaises(requests.ReadTimeout),
        ):
            adapter.publish(self.article, None, self.context)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["timeout"], client.WP_HTTP_TIMEOUT)

    def test_reconciliation_short_circuits_post_creation(self):
        reconciled = publisher.PublicationResult(
            success=True,
            external_id=101,
            reconciled=True,
        )
        adapter = publisher.WordPressPublisher()
        with (
            patch.object(adapter, "reconcile", return_value=reconciled),
            patch.object(publisher.session, "post") as post,
        ):
            result = adapter.publish(self.article, None, self.context)
        self.assertIs(result, reconciled)
        post.assert_not_called()

    def test_successful_post_returns_success_for_batch_state_update(self):
        result, _mocks, _connection, _events = self.publish_with_mocks()
        self.assertTrue(result.success)

    def test_failed_post_returns_before_usage_or_yoast(self):
        result, mocks, _connection, events = self.publish_with_mocks(
            post_response=response(status=500, text="Synthetic private server error token=secret"),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error, "post_create_http_status:500")
        self.assertNotIn("secret", result.error)
        self.assertNotIn("private server error", result.error)
        mocks[4].assert_not_called()
        mocks[5].assert_not_called()
        self.assertEqual(events, [])

    def test_missing_featured_image_still_publishes_without_media_field(self):
        result, mocks, _connection, _events = self.publish_with_mocks(image=None)
        self.assertTrue(result.success)
        self.assertNotIn("featured_media", mocks[2].call_args.kwargs["json"])

    def test_stock_usage_is_recorded_at_existing_success_point(self):
        metadata = {"provider": "openverse", "asset_id": "asset-1"}
        image = PublicationImage(external_id=77, metadata=metadata)
        result, mocks, _connection, events = self.publish_with_mocks(image=image)
        self.assertTrue(result.success)
        mocks[4].assert_called_once_with(metadata, 101, ARTICLE_ROW["title"])
        self.assertEqual(events, ["usage", "yoast"])

    def test_yoast_receives_current_article_values(self):
        _result, mocks, _wp_connection, _events = self.publish_with_mocks()
        mocks[5].assert_called_once_with(
            publisher.session,
            101,
            focus_keyword=ARTICLE_ROW["seo_focus"],
            description=ARTICLE_ROW["seo_meta"],
            title=ARTICLE_ROW["title"],
            canonical_url=ARTICLE_ROW["seo_canonical"],
        )


class AdvisoryLockTests(unittest.TestCase):
    def test_lock_contention_skips_without_fetching_or_releasing(self):
        lock_conn = MagicMock()
        lock_conn.is_connected.return_value = True
        lock_cursor = Mock()
        lock_cursor.fetchone.return_value = (444,)
        lock_conn.cursor.return_value = lock_cursor
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=lock_conn),
            patch.object(publisher, "_get_lock", return_value=False) as get_lock,
            patch.object(publisher, "_release_lock") as release_lock,
            patch.object(publisher, "_count_due_now") as count_due,
        ):
            result = publisher.publish_news_to_wp()
        self.assertEqual(result["status"], "skipped_already_running")
        get_lock.assert_called_once_with(lock_conn, "wp_publisher_lock", 1)
        lock_cursor.execute.assert_called_once_with(
            "SELECT IS_USED_LOCK(%s)",
            ("wp_publisher_lock",),
        )
        count_due.assert_not_called()
        release_lock.assert_not_called()
        lock_conn.close.assert_called_once_with()

    def test_acquired_lock_releases_on_empty_batch(self):
        lock_conn = MagicMock()
        lock_conn.is_connected.return_value = True
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=lock_conn),
            patch.object(publisher, "_get_lock", return_value=True) as get_lock,
            patch.object(publisher, "_release_lock", return_value=True) as release_lock,
            patch.object(publisher, "_count_due_now", return_value=0),
        ):
            result = publisher.publish_news_to_wp()
        self.assertEqual(result["status"], "no_work")
        get_lock.assert_called_once_with(lock_conn, "wp_publisher_lock", 1)
        release_lock.assert_called_once_with(lock_conn, "wp_publisher_lock")
        lock_conn.close.assert_called_once_with()

    def test_lock_sql_names_and_timeout_remain_equivalent(self):
        connection = Mock()
        cursor = Mock()
        connection.cursor.return_value = cursor
        cursor.fetchone.side_effect = [(1,), (1,)]
        self.assertTrue(publisher._get_lock(connection, "wp_publisher_lock", 1))
        self.assertTrue(publisher._release_lock(connection, "wp_publisher_lock"))
        self.assertEqual(
            cursor.execute.call_args_list,
            [
                call("SELECT GET_LOCK(%s,%s)", ("wp_publisher_lock", 1)),
                call("SELECT RELEASE_LOCK(%s)", ("wp_publisher_lock",)),
            ],
        )


class AttributionFlowTests(unittest.TestCase):
    @staticmethod
    def upload_selection(metadata, *, metadata_error=None, from_generation=False):
        selected_meta = dict(metadata)
        if selected_meta.get("provider") in {"openverse", "wikimedia"} and selected_meta.get("license_name") == "cc-by":
            selected_meta = {
                "creator_name": "Example Artist", "work_title": "Example Work",
                "source_page_url": "https://source.example/file",
                "license_version": "4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "attribution_complete": True, **selected_meta,
            }
        selected = (b"prepared", "image.jpg", selected_meta)
        details_response = Mock()
        details_response.raise_for_status.side_effect = metadata_error
        with (
            patch.object(publisher, "IMAGE_SEARCH_ENGINE", "v2"),
            patch.object(publisher, "IMAGE_REQUIRE_FEATURED_IMAGE", False),
            patch.object(publisher, "IMAGE_SOURCE_MODE", "hybrid"),
            patch.object(publisher, "IMAGE_SOURCE_PRIORITY", "stock_first"),
            patch.object(publisher, "USE_SOURCE_IMAGES", False),
            patch.object(
                publisher, "_search_stock_image_content",
                return_value=(None, True) if from_generation else (selected, False),
            ),
            patch.object(publisher, "_generated_image_content", return_value=selected) as generated,
            patch.object(publisher, "upload_media", return_value=88) as upload,
            patch.object(publisher.session, "post", return_value=details_response) as post,
        ):
            result = publisher.upload_image(
                None, ARTICLE_ROW["title"], ARTICLE_ROW["hashtags"], article=ARTICLE_ROW,
            )
        return result, upload, post, generated

    def test_long_cc_by_credit_and_change_notice_survive_upload(self):
        original = "Provider required notice. " * 70 + (
            "Creator: Example Artist. Source: https://source.example/file. "
            "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"
        )
        (media_id, metadata), _upload, post, generated = self.upload_selection({
            "provider": "openverse", "license_name": "cc-by",
            "credit_text": original, "attribution_text": original,
        })
        published = original + "\nChanges: cropped and resized for publication."
        self.assertEqual(media_id, 88)
        caption = post.call_args.kwargs["json"]["caption"]
        self.assertNotEqual(caption, published)
        for expected in ("Example Artist", "Example Work", "CC BY 4.0", "Cropped"):
            self.assertIn(expected, caption)
        self.assertNotIn("Provider required notice.", caption)
        self.assertEqual(post.call_args.kwargs["json"]["description"], published)
        self.assertEqual(metadata["provider_attribution_text"], original)
        self.assertEqual(metadata["attribution_text"], published)
        self.assertEqual(metadata["credit_text"], published)
        generated.assert_not_called()

    def test_generated_image_does_not_receive_a_cc_change_notice(self):
        (media_id, metadata), _upload, post, generated = self.upload_selection({
            "provider": "grok-image", "credit_text": "", "license_name": "cc-by",
        }, from_generation=True)
        self.assertEqual(media_id, 88)
        self.assertNotIn("provider_attribution_text", metadata)
        self.assertNotIn("caption", post.call_args.kwargs["json"])
        self.assertNotIn("description", post.call_args.kwargs["json"])
        generated.assert_called_once()

    def test_pexels_pixabay_credit_behavior_remains_compatible(self):
        for provider in ("pexels", "pixabay"):
            with self.subTest(provider=provider):
                (media_id, metadata), _upload, post, generated = self.upload_selection({
                    "provider": provider, "credit_text": "Original provider credit",
                })
                self.assertEqual(media_id, 88)
                self.assertEqual(metadata["credit_text"], "Original provider credit")
                self.assertEqual(post.call_args.kwargs["json"]["caption"], "Original provider credit")
                generated.assert_not_called()

    def test_v1_stock_metadata_failure_rejects_pexels_and_pixabay_without_grok(self):
        for provider in ("pexels", "pixabay"):
            with self.subTest(provider=provider):
                candidate = publisher.StockImageCandidate(
                    provider=provider,
                    image_url=f"https://images.example/{provider}.jpg",
                    query="bitcoin cryptocurrency",
                    score=0.99,
                    threshold=0.72 if provider == "pexels" else 0.78,
                    width=1600,
                    height=900,
                    credit_name="Example Photographer",
                    credit_url=f"https://{provider}.example/photo/123",
                    provider_asset_id="123",
                )
                details_response = Mock()
                details_response.raise_for_status.side_effect = requests.HTTPError("metadata unavailable")
                with (
                    patch.object(publisher, "IMAGE_SEARCH_ENGINE", "v1"),
                    patch.object(publisher, "IMAGE_SOURCE_MODE", "hybrid"),
                    patch.object(publisher, "IMAGE_SOURCE_PRIORITY", "stock_first"),
                    patch.object(publisher, "USE_SOURCE_IMAGES", False),
                    patch.object(publisher, "find_stock_image", return_value=candidate) as find,
                    patch.object(
                        publisher, "_download_external_hero_image",
                        return_value=(b"prepared", {"width": 1600, "height": 900}),
                    ) as download,
                    patch.object(publisher, "_stock_image_content_v2") as v2,
                    patch.object(publisher, "_generated_image_content") as generated,
                    patch.object(publisher, "_grok_image_content") as grok,
                    patch.object(publisher, "upload_media", return_value=88) as upload,
                    patch.object(publisher.session, "post", return_value=details_response) as details,
                    self.assertLogs(level="WARNING"),
                ):
                    media_id, metadata = publisher.upload_image(
                        None, ARTICLE_ROW["title"], ARTICLE_ROW["hashtags"], article=ARTICLE_ROW,
                    )

                find.assert_called_once_with(ARTICLE_ROW, providers=("pexels", "pixabay"))
                download.assert_called_once_with(candidate.image_url, provider)
                upload.assert_called_once()
                self.assertEqual(details.call_args.kwargs["json"]["caption"], candidate.credit_text)
                self.assertIsNone(media_id)
                self.assertEqual(metadata["provider"], provider)
                self.assertEqual(metadata["featured_image_rejected_reason"], "metadata_update_failed")
                v2.assert_not_called()
                generated.assert_not_called()
                grok.assert_not_called()

                with (
                    patch.object(publisher, "ensure_category", return_value=21),
                    patch.object(publisher, "ensure_term", return_value=31),
                    patch.object(publisher.session, "post", return_value=response(body={"id": 101})) as post,
                    patch.object(publisher, "write_yoast_metadata"),
                    patch.object(publisher, "record_image_usage") as record,
                    patch("builtins.print"),
                ):
                    result = publisher.WordPressPublisher().publish(
                        PublicationArticle.from_mapping(ARTICLE_ROW), None,
                        PublicationContext(published_at_utc=PUBLISHED_AT, creation_is_fresh=True),
                    )
                self.assertTrue(result.success)
                self.assertNotIn("featured_media", post.call_args.kwargs["json"])
                record.assert_not_called()

    def test_required_credit_failure_returns_no_featured_image_and_never_generates(self):
        with self.assertLogs(level="WARNING") as logs:
            (media_id, metadata), upload, _post, generated = self.upload_selection({
                "provider": "openverse", "license_name": "cc-by", "credit_text": "Complete credit",
            }, metadata_error=requests.HTTPError("secret-token request credentials"))
        self.assertIsNone(media_id)
        self.assertEqual(metadata["featured_image_rejected_reason"], "metadata_update_failed")
        upload.assert_called_once()
        generated.assert_not_called()
        self.assertNotIn("secret-token", " ".join(logs.output))

        with (
            patch.object(publisher, "ensure_category", return_value=21),
            patch.object(publisher, "ensure_term", return_value=31),
            patch.object(publisher.session, "post", return_value=response(body={"id": 101})) as post,
            patch.object(publisher, "write_yoast_metadata"),
            patch.object(publisher, "record_image_usage") as record,
            patch("builtins.print"),
        ):
            result = publisher.WordPressPublisher().publish(
                PublicationArticle.from_mapping(ARTICLE_ROW), None,
                PublicationContext(published_at_utc=PUBLISHED_AT, creation_is_fresh=True),
            )
        self.assertTrue(result.success)
        self.assertNotIn("featured_media", post.call_args.kwargs["json"])
        record.assert_not_called()

    def test_missing_or_oversized_stock_credit_rejects_before_media_upload(self):
        for credit, reason in (
            ("", "attribution_missing"),
            ("x" * media.MAX_REQUIRED_ATTRIBUTION_CHARS, "attribution_too_large"),
        ):
            with self.subTest(reason=reason):
                with self.assertLogs(publisher.logger, level="WARNING"):
                    (media_id, metadata), upload, post, generated = self.upload_selection({
                        "provider": "openverse", "license_name": "cc-by", "credit_text": credit,
                    })
                self.assertIsNone(media_id)
                self.assertEqual(metadata["featured_image_rejected_reason"], reason)
                upload.assert_not_called()
                post.assert_not_called()
                generated.assert_not_called()

    def test_openverse_attribution_reaches_wordpress_media_details_payload(self):
        raw = {
            "id": "openverse-phase-one",
            "foreign_identifier": "foreign-phase-one",
            "source": "flickr",
            "url": "https://live.staticflickr.com/phase-one.jpg",
            "foreign_landing_url": "https://flickr.com/photos/phase-one",
            "creator": "Phase One Creator",
            "creator_url": "https://flickr.com/people/phase-one",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "title": "Phase One Work",
        }
        candidate = OpenverseProvider(enabled=False, threshold=0.30).normalize_candidate(
            raw,
            "bitcoin markets",
            0,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        search_result = SimpleNamespace(
            candidate=candidate,
            downloaded=SimpleNamespace(
                content=b"downloaded",
                content_sha256="sha256",
                perceptual_hash="phash",
            ),
            all_available_providers_exhausted=False,
        )

        def search_with_preparation(_article, *, post_download_validator):
            self.assertTrue(post_download_validator(candidate, search_result.downloaded))
            return search_result

        media_response = response(body={"id": 88})
        details_response = Mock()
        with (
            patch.object(publisher, "IMAGE_SEARCH_ENGINE", "v2"),
            patch.object(publisher, "IMAGE_SOURCE_MODE", "hybrid"),
            patch.object(publisher, "IMAGE_SOURCE_PRIORITY", "stock_first"),
            patch.object(publisher, "USE_SOURCE_IMAGES", False),
            patch.object(publisher, "search_images", side_effect=search_with_preparation),
            patch.object(
                publisher,
                "_cover_bytes_to_1536x1024",
                return_value=(b"prepared", {"width": 1536, "height": 1024}),
            ),
            patch.object(publisher, "_post_with_retries", return_value=media_response),
            patch.object(publisher.session, "post", return_value=details_response) as post,
        ):
            media_id, metadata = publisher.upload_image(
                None,
                ARTICLE_ROW["title"],
                ARTICLE_ROW["hashtags"],
                article=ARTICLE_ROW,
            )

        self.assertEqual(media_id, 88)
        self.assertEqual(metadata["provider"], "openverse")
        payload = post.call_args.kwargs["json"]
        self.assertNotEqual(payload["caption"], payload["description"])
        for expected in (raw["title"], raw["creator"], "CC BY 4.0", "Cropped"):
            self.assertIn(expected, payload["caption"])
        for expected in (
            raw["title"],
            raw["creator"],
            raw["foreign_landing_url"],
            "CC BY 4.0",
            raw["license_url"],
            "Changes: cropped and resized for publication.",
        ):
            self.assertIn(expected, payload["description"])
        details_response.raise_for_status.assert_called_once_with()

    def test_image_search_engine_default_remains_v1(self):
        import config

        self.assertIn(
            'os.getenv("IMAGE_SEARCH_ENGINE", "v1")',
            inspect.getsource(config),
        )


if __name__ == "__main__":
    unittest.main()
