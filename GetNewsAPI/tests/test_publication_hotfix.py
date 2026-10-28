"""Exercise both production hotfixes through durable SQL and fake WordPress REST."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from duplicate_detection.enforcement import DUPLICATE_COUNTER_NAMES
from publishing.service import PublishingService
from publishing.wordpress import publisher
from repositories.publication import PublicationRepository
from tests.test_yoast_metadata_sync import (
    FakeResponse,
    SQLitePublicationConnection,
    create_restart_database,
)


PRODUCTION_DESCRIPTION = (
    "The recent  Strive bitcoin buy  stands out as one of the largest single "
    "acquisitions in recent months. Strive Inc. purchased 3,311 BTC for roughly 280"
)
WORDPRESS_ORIGIN = "https://wordpress.example.invalid"


class BatchWordPress:
    """Return independent stored metadata per existing post, with no remote I/O."""

    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.updates: list[int] = []
        self.creates: list[str] = []
        self.metadata: dict[int, dict[str, str]] = {}

    def get(self, url: str, **_kwargs) -> FakeResponse:
        post_id = int(url.rsplit("/", 1)[1])
        return FakeResponse({
            "id": post_id,
            "link": f"{WORDPRESS_ORIGIN}/posts/{post_id}/",
            "type": "post", "status": "publish",
            "meta": dict(self.metadata.get(post_id, {})),
        })

    def post(self, url: str, **kwargs) -> FakeResponse:
        if url.rstrip("/").endswith("/posts"):
            self.creates.append(url)
            raise AssertionError("Existing posts must never be created again")
        post_id = int(url.rsplit("/", 1)[1])
        self.updates.append(post_id)
        if post_id == 5303 and self.failure == "rest_error":
            return FakeResponse({"code": "synthetic_rejection"}, 503)
        intended = dict(kwargs["json"]["meta"])
        intended["_yoast_wpseo_metadesc"] = re.sub(
            r" {2,}", " ", intended["_yoast_wpseo_metadesc"],
        )
        if post_id == 5303 and self.failure == "numeric_mismatch":
            intended["_yoast_wpseo_metadesc"] = intended["_yoast_wpseo_metadesc"].replace(
                "3,311 BTC", "3,312 BTC",
            )
        self.metadata[post_id] = intended
        return self.get(url)


class DurablePublicationHotfixTests(unittest.TestCase):
    def test_failed_existing_post_yields_batch_slots_and_retries_next_invocation(self):
        for failure in ("numeric_mismatch", "rest_error"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "application.sqlite")
                create_restart_database(path)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("DELETE FROM rich_crpytonews")
                    connection.execute("DELETE FROM cryptonewsapi")
                    for rich_id, raw_id, post_id in ((445, 15291, 5303), (446, 15292, 5304), (447, 15293, 5305)):
                        source = f"https://source.example.invalid/news/{raw_id}"
                        description = PRODUCTION_DESCRIPTION if rich_id != 447 else "An exact description."
                        connection.execute(
                            """INSERT INTO rich_crpytonews
                               (id, raw_article_id, news_url, title, full_text,
                                category, hashtags, seo_slug, seo_focus, seo_meta,
                                published, publish_status, publish_attempt_count,
                                wp_post_id, wp_post_url)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (rich_id, raw_id, source, "Strive Bitcoin Buy Dwarfs Strategy Purchase",
                             "<p>A factual body.</p>", "Markets", "", f"strive-{rich_id}",
                             "strive bitcoin buy", description, 0, "retryable", 31,
                             post_id, f"{WORDPRESS_ORIGIN}/posts/{post_id}/"),
                        )
                        connection.execute(
                            """INSERT INTO cryptonewsapi
                               (id, news_url, chosen_for_publish, scheduled_for, is_breaking)
                               VALUES (?, ?, ?, ?, ?)""",
                            (raw_id, source, 1, "2026-01-01 00:00:00", 0),
                        )
                    connection.commit()

                repository = PublicationRepository(connect=lambda: SQLitePublicationConnection(path))
                adapter = publisher.WordPressPublisher()
                image = Mock(side_effect=AssertionError("Existing posts must reconcile before images"))
                http = BatchWordPress(failure)
                service = PublishingService(repository, adapter, image, fresh_start_after=None)
                with (
                    patch.object(publisher, "session", http),
                    patch.object(publisher, "WP_API_URL", WORDPRESS_ORIGIN),
                    patch.object(publisher.mysql.connector, "connect",
                                 side_effect=AssertionError("WordPress SQL must not be used")),
                    self.assertLogs(publisher.logger, level="ERROR"),
                ):
                    first = service.publish_due(3)
                with closing(sqlite3.connect(path)) as connection:
                    rows = connection.execute(
                        """SELECT id, published, publish_status, publish_attempt_count,
                                  wp_post_id, wp_post_url, publish_claim_token, seo_meta
                           FROM rich_crpytonews ORDER BY id""",
                    ).fetchall()

                self.assertEqual(first, {"attempted": 3, "succeeded": 2, "failed": 1,
                                        "duplicates_rejected": 0, "duplicate_guard_deferred": 0,
                                        **dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0)})
                self.assertEqual(http.updates, [5303, 5304, 5305])
                self.assertEqual(rows[0][:5], (445, 0, "retryable", 32, 5303))
                self.assertEqual(rows[0][5], f"{WORDPRESS_ORIGIN}/posts/5303/")
                self.assertIsNone(rows[0][6])
                self.assertEqual(rows[0][7], PRODUCTION_DESCRIPTION)
                self.assertEqual(rows[1][:5], (446, 1, "published", 32, 5304))
                self.assertEqual(rows[1][7], PRODUCTION_DESCRIPTION)
                self.assertEqual(rows[2][:5], (447, 1, "published", 32, 5305))

                http.failure = "repaired"
                with (
                    patch.object(publisher, "session", http),
                    patch.object(publisher, "WP_API_URL", WORDPRESS_ORIGIN),
                    patch.object(publisher.mysql.connector, "connect",
                                 side_effect=AssertionError("WordPress SQL must not be used")),
                ):
                    second = service.publish_due(3)
                with closing(sqlite3.connect(path)) as connection:
                    state = connection.execute(
                        """SELECT published, publish_status, publish_attempt_count,
                                  wp_post_id, wp_post_url, seo_meta
                           FROM rich_crpytonews WHERE id = 445""",
                    ).fetchone()
                self.assertEqual(second, {"attempted": 1, "succeeded": 1, "failed": 0,
                                         "duplicates_rejected": 0, "duplicate_guard_deferred": 0,
                                         **dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0)})
                self.assertEqual(http.updates, [5303, 5304, 5305, 5303])
                self.assertEqual(state, (1, "published", 33, 5303,
                                        f"{WORDPRESS_ORIGIN}/posts/5303/", PRODUCTION_DESCRIPTION))
                self.assertEqual(http.creates, [])
                image.assert_not_called()
