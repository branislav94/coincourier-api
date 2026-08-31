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
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=lock_conn),
            patch.object(publisher, "_get_lock", return_value=True),
            patch.object(publisher, "_release_lock", return_value=True),
            patch.object(publisher, "_count_due_now", return_value=1),
            patch.object(publisher, "fetch_unpublished", return_value=[ARTICLE_ROW]),
            patch.object(publisher, "upload_image", return_value=(None, {})),
            patch.object(publisher, "mark_news_as_published") as mark_published,
            patch.object(publisher, "WordPressPublisher", return_value=adapter) as adapter_type,
        ):
            adapter.publish.return_value = publisher.PublicationResult(
                success=True,
                external_id=101,
            )
            result = publish_to_wp.publish_news_to_wp()

        self.assertIsNone(result)
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
            patch.object(publisher, "upload_image", return_value=(None, {})),
            patch.object(publisher, "mark_news_as_published") as mark_published,
            patch.object(publisher, "WordPressPublisher", return_value=adapter),
        ):
            publish_to_wp.publish_news_to_wp()
        mark_published.assert_not_called()

    def test_tasks_resolves_same_publish_entry_point(self):
        with patch.object(publish_to_wp, "publish_news_to_wp", return_value=None) as publish:
            tasks.run_publish()
        publish.assert_called_once_with()

    def test_scheduler_resolves_same_publish_entry_point(self):
        fake_fetcher = types.ModuleType("fetcher")
        fake_fetcher.start_scheduler = Mock()
        fake_fetcher.stop_scheduler = Mock()
        fake_processor = types.ModuleType("gpt_processor")
        fake_processor.process_news_with_gpt = Mock()

        spec = importlib.util.spec_from_file_location("scheduler_contract_test", SCHEDULER_PATH)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        scheduler_module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                "fetcher": fake_fetcher,
                "gpt_processor": fake_processor,
                "publish_to_wp": publish_to_wp,
            },
        ):
            spec.loader.exec_module(scheduler_module)

        self.assertIs(
            scheduler_module.publish_news_to_wp,
            publish_to_wp.publish_news_to_wp,
        )

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
        )

    def test_new_media_owner_patch_controls_legacy_helper(self):
        with patch.object(media, "set_media_details") as details:
            publisher.set_media_details(20, "Alt", caption="Credit")
        details.assert_called_once_with(
            20,
            "Alt",
            caption="Credit",
            description=None,
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

    def test_media_retry_preserves_request_exception_behavior(self):
        http_session = Mock()
        http_session.post.side_effect = [
            requests.ConnectionError("temporary"),
            response(status=201),
        ]
        with (
            patch.object(client.time, "sleep") as sleep,
            patch("builtins.print"),
        ):
            result = client.post_with_retries(http_session, "https://wp.example/media")
        self.assertEqual(result.status_code, 201)
        sleep.assert_called_once_with(10)

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
        )
        http_response.raise_for_status.assert_called_once_with()


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
        )
        post.assert_called_once_with(
            f"{taxonomy.API_BASE}/wp-json/wp/v2/tags",
            json={"name": "Bitcoin ETF", "slug": "bitcoin-etf"},
        )


class SeoAndDatabaseTests(unittest.TestCase):
    def test_yoast_metadata_writes_remain_equivalent(self):
        connection = Mock()
        with (
            patch.object(seo, "get_wp_prefix", return_value="site_") as prefix,
            patch.object(seo, "upsert_postmeta") as upsert,
        ):
            seo.write_yoast_metadata(
                connection,
                91,
                focus_keyword="focus",
                description="d" * 170,
                title="t" * 70,
                canonical_url="https://canonical.example/post",
            )
        prefix.assert_called_once_with(connection)
        self.assertEqual(
            upsert.call_args_list,
            [
                call(connection, "site_", 91, "_yoast_wpseo_focuskw", "focus"),
                call(connection, "site_", 91, "_yoast_wpseo_metadesc", "d" * 160),
                call(connection, "site_", 91, "_yoast_wpseo_title", "t" * 58),
                call(
                    connection,
                    "site_",
                    91,
                    "_yoast_wpseo_canonical",
                    "https://canonical.example/post",
                ),
            ],
        )

    def test_canonical_yoast_metadata_remains_conditional(self):
        with (
            patch.object(seo, "get_wp_prefix", return_value="wp_"),
            patch.object(seo, "upsert_postmeta") as upsert,
        ):
            seo.write_yoast_metadata(
                Mock(),
                92,
                focus_keyword="focus",
                description="description",
                title="title",
                canonical_url=None,
            )
        self.assertEqual(upsert.call_count, 3)
        self.assertNotIn(
            "_yoast_wpseo_canonical",
            [item.args[3] for item in upsert.call_args_list],
        )

    def test_postmeta_upsert_sql_and_commit_remain_equivalent(self):
        connection = Mock()
        cursor = Mock()
        connection.cursor.return_value = cursor
        seo.upsert_postmeta(connection, "wp7_", 93, "_yoast_wpseo_title", "Title")
        sql, params = cursor.execute.call_args.args
        self.assertIn("INSERT INTO wp7_postmeta", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)", sql)
        self.assertEqual(params, (93, "_yoast_wpseo_title", "Title"))
        connection.commit.assert_called_once_with()
        cursor.close.assert_called_once_with()

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
        self.assertIn("c.chosen_for_publish = 1", sql)
        self.assertIn("c.scheduled_for <= UTC_TIMESTAMP()", sql)
        self.assertIn("ORDER BY c.scheduled_for ASC", sql)
        self.assertEqual(params, (7,))


class WordPressPublisherTests(unittest.TestCase):
    def setUp(self):
        self.article = PublicationArticle.from_mapping(ARTICLE_ROW)
        self.context = PublicationContext(published_at_utc=PUBLISHED_AT)

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

    def test_successful_post_returns_success_for_batch_state_update(self):
        result, _mocks, _connection, _events = self.publish_with_mocks()
        self.assertTrue(result.success)

    def test_failed_post_returns_before_usage_or_yoast(self):
        result, mocks, _connection, events = self.publish_with_mocks(
            post_response=response(status=500, text="failed"),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error, "failed")
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
        _result, mocks, wp_connection, _events = self.publish_with_mocks()
        mocks[5].assert_called_once_with(
            wp_connection,
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
            publisher.publish_news_to_wp()
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
            publisher.publish_news_to_wp()
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
        self.assertEqual(payload["caption"], payload["description"])
        for expected in (
            raw["title"],
            raw["creator"],
            raw["foreign_landing_url"],
            "CC BY 4.0",
            raw["license_url"],
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
