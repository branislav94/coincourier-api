"""Opt-in native transaction checks, restricted to disposable loopback MariaDB.

Uses the existing RUN_MARIADB_INTEGRATION / MARIADB_TEST_* infrastructure.
Pipeline composition installs synthetic configuration and mocks WordPress;
no production credentials or external API is used.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from repositories.publication_capacity import (
    CapacityAlreadyAttempted,
    CapacityRepository,
    CapacityUnavailable,
)


RUN_INTEGRATION = os.getenv("RUN_MARIADB_INTEGRATION", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
HOST = os.getenv("MARIADB_TEST_HOST", "127.0.0.1")
PORT = int(os.getenv("MARIADB_TEST_PORT", "13307"))
DATABASE = os.getenv("MARIADB_TEST_DATABASE", "coincourier_api_test")
USER = os.getenv("MARIADB_TEST_USER", "getnewsapi_test")
PASSWORD = os.getenv("MARIADB_TEST_PASSWORD", "getnewsapi_test_only")
MIGRATION = PROJECT_DIR.parent / "maintenance" / "migrations" / "008_publication_daily_capacity.sql"


def _assert_disposable():
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("publication capacity integration host must be loopback")
    if not DATABASE.endswith("_test"):
        raise RuntimeError("publication capacity integration database must end in _test")


def _connect(*, at: datetime | None = None, autocommit=False):
    _assert_disposable()
    connection = mysql.connector.connect(
        host=HOST,
        port=PORT,
        database=DATABASE,
        user=USER,
        password=PASSWORD,
        connection_timeout=10,
        ssl_disabled=True,
        autocommit=autocommit,
    )
    if at is not None:
        cursor = connection.cursor()
        try:
            # MariaDB's session clock gives deterministic boundary tests without
            # changing the server clock or accepting a caller clock in the API.
            cursor.execute("SET timestamp = %s", (int(at.replace(tzinfo=timezone.utc).timestamp()),))
        finally:
            cursor.close()
    return connection


@unittest.skipUnless(RUN_INTEGRATION, "set RUN_MARIADB_INTEGRATION=true for disposable MariaDB tests")
class PublicationCapacityMariaDBTests(unittest.TestCase):
    def setUp(self):
        _assert_disposable()
        self.apply_migration()
        self.execute("DELETE FROM publication_capacity_reservations")
        self.execute("DELETE FROM publication_capacity_days")

    def apply_migration(self, path=MIGRATION):
        connection = _connect(autocommit=True)
        cursor = connection.cursor()
        try:
            cursor.execute(path.read_text(encoding="utf-8"), map_results=True)
            while True:
                if cursor.with_rows:
                    cursor.fetchall()
                if not cursor.nextset():
                    break
        finally:
            cursor.close()
            connection.close()

    def execute(self, sql, params=()):
        connection = _connect(autocommit=True)
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return list(cursor.fetchall()) if cursor.with_rows else cursor.rowcount
        finally:
            cursor.close()
            connection.close()

    def repository(self, *, at=None):
        return CapacityRepository(30, connect=lambda: _connect(at=at))

    def test_twenty_nine_slots_allow_one_native_concurrent_create(self):
        repository = self.repository()
        for article_id in range(1, 30):
            self.assertIsNotNone(repository.begin_create(article_id, f"article:{article_id}"))
        barrier = threading.Barrier(8)

        def create(article_id):
            barrier.wait(timeout=10)
            return self.repository().begin_create(article_id, f"article:{article_id}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create, range(30, 38)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM publication_capacity_reservations"), [(30,)])
        self.assertEqual(repository.snapshot().remaining, 0)
        self.assertIsNone(self.repository().begin_create(40, "article:40"))

    def test_same_identity_race_consumes_one_slot_and_authorizes_one_create(self):
        barrier = threading.Barrier(6)

        def reserve(_):
            barrier.wait(timeout=10)
            return self.repository().reserve(1, "article:1")

        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertTrue(all(executor.map(reserve, range(6))))
        self.assertEqual(self.execute("SELECT COUNT(*) FROM publication_capacity_reservations"), [(1,)])
        barrier = threading.Barrier(6)

        def create(_):
            barrier.wait(timeout=10)
            try:
                return self.repository().begin_create(1, "article:1")
            except CapacityAlreadyAttempted:
                return None

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(create, range(6)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.repository().snapshot().remaining, 29)

    def test_midnight_moves_unused_reservation_and_persists_exact_wordpress_time(self):
        before = datetime(2026, 10, 15, 23, 59, 59)
        after = datetime(2026, 10, 16, 0, 0, 0)
        first = self.repository(at=before).reserve(1, "article:1")
        self.assertEqual(first.utc_date, before.date())
        stamp = self.repository(at=after).begin_create(1, "article:1")
        self.assertEqual(stamp, after.replace(tzinfo=timezone.utc))
        self.assertEqual(
            self.execute("SELECT `utc_date`, publication_at, create_started_at FROM publication_capacity_reservations WHERE rich_article_id = 1"),
            [(after.date(), after, after)],
        )
        self.assertEqual(self.repository(at=before).snapshot().remaining, 30)
        self.assertEqual(self.repository(at=after).snapshot().remaining, 29)

    def test_midnight_full_day_keeps_carryover_unattempted_and_queued(self):
        before = datetime(2026, 10, 15, 23, 59, 59)
        after = datetime(2026, 10, 16, 0, 0, 0)
        self.repository(at=before).reserve(1, "article:1")
        self.execute(
            "INSERT INTO publication_capacity_days (`utc_date`, hard_cap, baseline_count) VALUES (%s, 30, 30)",
            (after.date(),),
        )
        self.assertIsNone(self.repository(at=after).begin_create(1, "article:1"))
        self.assertEqual(
            self.execute("SELECT `utc_date`, create_started_at FROM publication_capacity_reservations WHERE rich_article_id = 1"),
            [(before.date(), None)],
        )

    def test_confirmed_and_ambiguous_creation_remain_fenced_after_midnight(self):
        before = datetime(2026, 10, 15, 23, 59, 59)
        after = datetime(2026, 10, 16, 0, 0, 0)
        repository = self.repository(at=before)
        repository.begin_create(1, "article:1")
        repository.record_created(1, "article:1", 1001)
        repository.begin_create(2, "article:2")  # Simulated ambiguous HTTP result.
        for article_id in (1, 2):
            with self.assertRaises(CapacityAlreadyAttempted):
                self.repository(at=after).begin_create(article_id, f"article:{article_id}")
        self.repository(at=after).record_created(1, "article:1", 1001)
        self.assertEqual(self.repository(at=after).snapshot().remaining, 30)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM publication_capacity_reservations"), [(2,)])

    def test_reapplying_additive_migration_preserves_reservations_and_baseline(self):
        self.repository().reserve(1, "article:1")
        self.execute("UPDATE publication_capacity_days SET baseline_count = 7")
        self.apply_migration()
        self.assertEqual(self.repository().snapshot().remaining, 22)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM publication_capacity_reservations"), [(1,)])

    def test_schema_with_missing_identity_constraint_fails_closed(self):
        self.execute("ALTER TABLE publication_capacity_reservations DROP INDEX uq_publication_capacity_key")
        try:
            with self.assertRaises(CapacityUnavailable):
                self.repository().begin_create(1, "article:1")
            self.assertEqual(self.execute("SELECT COUNT(*) FROM publication_capacity_reservations"), [(0,)])
        finally:
            self.execute("ALTER TABLE publication_capacity_reservations ADD UNIQUE KEY uq_publication_capacity_key (publication_key)")

    def test_native_claims_prioritize_breaking_carryover_and_reconcile_seo_at_full_capacity(self):
        from unittest.mock import Mock, patch
        from GetNewsAPI.tests.environment_isolation import install_test_environment

        install_test_environment()
        from GetNewsAPI.tests.test_publication_capacity_publishing import (
            WordPressHTTP, ORIGIN,
        )
        from publishing.service import PublishingService
        from publishing.wordpress import publisher
        from repositories.publication import PublicationRepository

        maintenance = PROJECT_DIR.parent / 'maintenance'
        self.apply_migration(maintenance / 'testing' / 'mariadb_phase2_baseline.sql')
        self.apply_migration(maintenance / 'migrations' / '002_phase2_durable_state.sql')
        self.apply_migration(maintenance / 'migrations' / '004_phase2_indexes.sql')
        self.execute('''
            INSERT INTO publication_capacity_days (`utc_date`, hard_cap, baseline_count)
            VALUES (UTC_DATE(), 30, 29)
        ''')
        for raw_id in (1, 2):
            source = f'https://source.example.invalid/{raw_id}'
            self.execute('''
                INSERT INTO cryptonewsapi
                    (id, news_url, title, publish_date, chosen_for_publish,
                     selected_at, scheduled_for, is_breaking)
                VALUES (%s, %s, 'Synthetic source', TIMESTAMPADD(HOUR, -1, UTC_TIMESTAMP()),
                        1, TIMESTAMPADD(DAY, -1, UTC_TIMESTAMP()),
                        TIMESTAMPADD(DAY, -1, UTC_TIMESTAMP()), %s)
            ''', (raw_id, source, int(raw_id == 2)))
            self.execute('''
                INSERT INTO rich_crpytonews
                    (id, raw_article_id, news_url, title, full_text, category,
                     hashtags, seo_focus, seo_meta, publish_status)
                VALUES (%s, %s, %s, 'Synthetic headline', '<p>Factual article.</p>',
                        'Markets', '', 'Bitcoin news', 'A factual description.', 'fresh')
            ''', (100 + raw_id, raw_id, source))

        capacity = self.repository()
        repository = PublicationRepository(connect=_connect)
        adapter = publisher.WordPressPublisher()
        adapter.find_external_media = Mock(return_value=None)
        image = Mock(return_value=(None, {}))
        service = PublishingService(
            repository, adapter, image, capacity_repository=capacity,
            capacity_enabled=True, fresh_start_after=None, duplicate_mode='off',
        )
        http = WordPressHTTP()
        http.metadata_failure = True
        with (
            patch.object(publisher, 'session', http),
            patch.object(publisher, 'WP_API_URL', ORIGIN),
            patch.object(publisher, 'PUBLISH_DAILY_HARD_CAP_ENABLED', False),
            patch.object(publisher, 'ensure_category', return_value=1),
            patch.object(publisher, 'ensure_term', return_value=2),
            patch.object(publisher, 'link_markets', side_effect=lambda value: value),
        ):
            first = service.publish_due(3)
            self.assertEqual(first['failed'], 1)
            self.assertEqual(self.execute(
                'SELECT rich_article_id, wp_post_id FROM publication_capacity_reservations'
            ), [(102, 501)])
            http.metadata_failure = False
            second = service.publish_due(3)
            third = service.publish_due(3)

        self.assertEqual(second['succeeded'], 1)
        self.assertEqual(third['attempted'], 0)
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(image.call_count, 1)
        self.assertEqual(capacity.snapshot().remaining, 0)
        self.assertEqual(self.execute(
            'SELECT id, published FROM rich_crpytonews ORDER BY id'
        ), [(101, 0), (102, 1)])
        persisted = self.execute('SELECT publication_at FROM publication_capacity_reservations')[0][0]
        self.assertEqual(http.creates[0]['date_gmt'], persisted.strftime('%Y-%m-%dT%H:%M:%S'))

    def test_final_native_duplicate_guard_rejects_after_reservation_before_create(self):
        from unittest.mock import Mock, patch
        from GetNewsAPI.tests.environment_isolation import install_test_environment

        install_test_environment()
        import config
        from duplicate_detection.publication import PublicationDuplicateGuard
        from GetNewsAPI.tests.test_publication_capacity_publishing import WordPressHTTP, ORIGIN
        from publishing.service import PublishingService
        from publishing.wordpress import publisher
        from repositories.duplicate_assessments import DuplicateAssessmentRepository
        from repositories.publication import PublicationRepository

        maintenance = PROJECT_DIR.parent / 'maintenance'
        for relative in (
            'testing/mariadb_phase2_baseline.sql',
            'migrations/002_phase2_durable_state.sql',
            'migrations/004_phase2_indexes.sql',
            'migrations/007_phase5_duplicate_shadow.sql',
        ):
            self.apply_migration(maintenance / relative)
        self.execute('''
            INSERT INTO publication_capacity_days (`utc_date`, hard_cap, baseline_count)
            VALUES (UTC_DATE(), 30, 29)
        ''')
        for raw_id in (1, 2):
            body = (
                'Solana validators activate Alpenglow mainnet on October 28, 2026 with 1000 transactions.'
                if raw_id == 1 else
                'Alpenglow mainnet activation on Solana occurs on October 28, 2026 with 1000 transactions. '
                'Validators confirm protocol details.'
            )
            self.execute('''
                INSERT INTO cryptonewsapi
                    (id, news_id, event_id, news_url, title, full_text, publish_date,
                     source_name, processed, chosen_for_publish, selected_at, scheduled_for)
                VALUES (%s, %s, %s, %s, 'Solana Alpenglow mainnet activation', %s,
                        UTC_TIMESTAMP(), %s, 1, 1, UTC_TIMESTAMP(),
                        TIMESTAMPADD(MINUTE, -1, UTC_TIMESTAMP()))
            ''', (raw_id, f'source-{raw_id}', f'different-provider-event-{raw_id}',
                  f'https://source.example.invalid/{raw_id}', body, f'Synthetic outlet {raw_id}'))
        self.execute('''
            INSERT INTO rich_crpytonews
                (id, raw_article_id, news_url, title, full_text, category,
                 hashtags, seo_focus, seo_meta, publish_status)
            VALUES (101, 1, 'https://source.example.invalid/1',
                    'Solana Alpenglow mainnet activation', '<p>Synthetic rewritten article.</p>',
                    'Markets', '', 'Solana news', 'A factual description.', 'fresh')
        ''')

        def reveal_external_canonical(_row):
            # Model an independently confirmed post appearing after the first
            # policy check and reservation, before the adapter's final check.
            self.assertEqual(self.execute('''
                SELECT rich_article_id, create_started_at
                FROM publication_capacity_reservations
            '''), [(101, None)])
            self.execute('''
                INSERT INTO rich_crpytonews
                    (id, raw_article_id, news_url, title, full_text, published,
                     publish_status, wp_post_id, wp_post_url, published_at)
                VALUES (102, 2, 'https://source.example.invalid/2',
                        'Solana Alpenglow mainnet activation', '<p>Confirmed canonical.</p>',
                        1, 'published', 901, 'https://wordpress.example.invalid/canonical/901',
                        UTC_TIMESTAMP())
            ''')
            return None, {}

        def duplicate_guard(raw_id):
            return PublicationDuplicateGuard(
                raw_id, mode='enforce',
                repository=DuplicateAssessmentRepository(connect=_connect),
                connection_factory=_connect,
            )

        capacity = self.repository()
        adapter = publisher.WordPressPublisher()
        adapter.find_external_media = Mock(return_value=None)
        image = Mock(side_effect=reveal_external_canonical)
        service = PublishingService(
            PublicationRepository(connect=_connect), adapter, image,
            capacity_repository=capacity, capacity_enabled=True,
            duplicate_mode='enforce', duplicate_guard_factory=duplicate_guard,
            fresh_start_after=None,
        )
        http = WordPressHTTP()
        with (
            patch.dict(config.DB_CONFIG, {'database': DATABASE}),
            patch.object(publisher, 'session', http),
            patch.object(publisher, 'WP_API_URL', ORIGIN),
            patch.object(publisher, 'PUBLISH_DAILY_HARD_CAP_ENABLED', False),
            patch.object(publisher, 'ensure_category', return_value=1),
            patch.object(publisher, 'ensure_term', return_value=2),
            patch.object(publisher, 'link_markets', side_effect=lambda value: value),
        ):
            result = service.publish_due(2)
            retry = service.publish_due(2)

        self.assertEqual(result['duplicates_rejected'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['succeeded'], 0)
        self.assertEqual(retry['attempted'], 0)
        self.assertEqual(image.call_count, 1)
        self.assertEqual(http.creates, [])
        self.assertEqual(capacity.snapshot().remaining, 0)
        self.assertEqual(self.execute('''
            SELECT rich_article_id, create_started_at, publication_at, wp_post_id
            FROM publication_capacity_reservations
        '''), [(101, None, None, None)])
        self.assertEqual(self.execute('''
            SELECT publish_status, published, wp_post_id
            FROM rich_crpytonews WHERE id = 101
        '''), [('duplicate', 0, None)])
        self.assertEqual(self.execute('''
            SELECT chosen_for_publish FROM cryptonewsapi WHERE id = 1
        '''), [(0,)])
        self.assertEqual(self.execute('''
            SELECT COUNT(*) FROM duplicate_assessments
            WHERE article_id = 1 AND candidate_article_id = 2
        '''), [(1,)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
