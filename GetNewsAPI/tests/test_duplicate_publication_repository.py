"""Run publication duplicate transitions against transactional in-memory SQL."""

from dataclasses import replace
import sqlite3
import unittest

from duplicate_detection.enforcement import DuplicateDecision
from publishing.models import is_fresh_publication_state
from repositories.publication import PublicationRepository
from repositories.raw_news import RawNewsRepository


class Cursor:
    def __init__(self, connection, *, dictionary=False):
        self.connection = connection
        self.cursor = connection.database.cursor()
        self.dictionary = dictionary

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.events.append(("execute", normalized, params))
        if self.connection.before_execute:
            self.connection.before_execute(normalized, params)
        sql = sql.replace("TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP())", "datetime('now', '-' || ? || ' minutes')")
        sql = sql.replace("UTC_TIMESTAMP()", "CURRENT_TIMESTAMP").replace("FOR UPDATE", "").replace("%s", "?")
        self.cursor.execute(sql, params)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None and self.dictionary else row

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def close(self):
        self.connection.events.append("cursor-close")
        self.cursor.close()


class Connection:
    def __init__(self, database, events, before_execute=None):
        self.database = database
        self.events = events
        self.before_execute = before_execute

    def start_transaction(self):
        self.events.append("begin")
        self.database.execute("BEGIN")

    def cursor(self, dictionary=False):
        return Cursor(self, dictionary=dictionary)

    def commit(self):
        self.events.append("commit")
        self.database.commit()

    def rollback(self):
        self.events.append("rollback")
        self.database.rollback()

    def close(self):
        self.events.append("close")


class DuplicatePublicationRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        self.database.executescript("""
            CREATE TABLE cryptonewsapi (
                id INTEGER PRIMARY KEY, news_url TEXT, processed INTEGER DEFAULT 1,
                chosen_for_publish INTEGER DEFAULT 1, scheduled_for TEXT,
                insertDate TEXT DEFAULT '2026-10-28', is_breaking INTEGER DEFAULT 0,
                processing_status TEXT DEFAULT 'completed', processing_claim_token TEXT,
                processing_claimed_at TEXT, processing_last_error TEXT
            );
            CREATE TABLE rich_crpytonews (
                id INTEGER PRIMARY KEY, news_url TEXT, raw_article_id INTEGER,
                published INTEGER DEFAULT 0, publish_status TEXT DEFAULT 'fresh',
                publish_claim_token TEXT, publish_claimed_at TEXT,
                publish_attempt_count INTEGER DEFAULT 0, publish_last_error TEXT,
                wp_post_id INTEGER, wp_post_url TEXT, wp_post_created_at TEXT,
                published_at TEXT, wp_media_id INTEGER, wp_media_metadata_json TEXT
            );
            INSERT INTO cryptonewsapi (id, news_url, scheduled_for) VALUES
                (101, 'https://news.example.invalid/current', '2020-01-01'),
                (17, 'https://news.example.invalid/canonical', '2020-01-01');
            INSERT INTO rich_crpytonews (id, raw_article_id, news_url) VALUES
                (201, 101, 'https://news.example.invalid/current');
            INSERT INTO rich_crpytonews
                (id, raw_article_id, news_url, published, publish_status, wp_post_id, wp_post_url)
                VALUES (27, 17, 'https://news.example.invalid/canonical', 1, 'published',
                        37, 'https://site.example.invalid/canonical');
        """)
        self.events = []
        self.before_execute = None
        self.repository = PublicationRepository(connect=lambda: Connection(
            self.database, self.events, self.before_execute,
        ))
        self.decision = DuplicateDecision(
            reject=True, available=True, mode="enforce", stage="pre_publish", article_id=101,
            canonical_raw_id=17, canonical_rich_id=27, canonical_wp_id=37,
        )

    def row(self, table="rich_crpytonews", row_id=201):
        self.assertIn(table, {"rich_crpytonews", "cryptonewsapi"})
        return dict(self.database.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())

    def update(self, *, table="rich_crpytonews", row_id=201, **changes):
        self.assertIn(table, {"rich_crpytonews", "cryptonewsapi"})
        columns = set(self.row(table, row_id))
        self.assertTrue(set(changes).issubset(columns))
        self.database.execute(
            f"UPDATE {table} SET " + ", ".join(f"{name} = ?" for name in changes) + " WHERE id = ?",
            (*changes.values(), row_id),
        )
        self.database.commit()

    def claimed(self, *, attempt=1):
        self.update(publish_status="claimed", publish_claim_token="owner", publish_claimed_at="2026-10-28",
                    publish_attempt_count=attempt, publish_last_error="previous controlled error")

    def reject(self, *, token=None, decision=None):
        return self.repository.reject_duplicate(201, 101, decision or self.decision, token=token)

    def assert_terminal(self):
        rich = self.row()
        raw = self.row("cryptonewsapi", 101)
        self.assertEqual((rich["published"], rich["publish_status"]), (0, "duplicate"))
        for field in ("publish_claim_token", "publish_claimed_at", "publish_last_error",
                      "wp_post_id", "wp_post_url", "wp_post_created_at"):
            self.assertIsNone(rich[field], field)
        self.assertEqual((raw["processed"], raw["chosen_for_publish"], raw["processing_status"]),
                         (1, 0, "completed"))
        for field in ("scheduled_for", "processing_claim_token", "processing_claimed_at", "processing_last_error"):
            self.assertIsNone(raw[field], field)

    def test_owned_rejection_commits_both_rows_once_and_preserves_published_canonical(self):
        self.claimed()
        canonical = self.row(row_id=27)
        canonical_raw = self.row("cryptonewsapi", 17)
        self.assertTrue(self.reject(token="owner"))
        self.assert_terminal()
        self.assertEqual(self.row(row_id=27), canonical)
        self.assertEqual(self.row("cryptonewsapi", 17), canonical_raw)
        self.assertEqual(self.events.count("begin"), 1)
        self.assertEqual(self.events.count("commit"), 1)
        self.assertNotIn("rollback", self.events)
        updates = [item[1] for item in self.events if isinstance(item, tuple) and item[1].startswith("UPDATE")]
        self.assertTrue(updates[0].startswith("UPDATE rich_crpytonews"))
        self.assertTrue(updates[1].startswith("UPDATE cryptonewsapi"))
        self.assertEqual(self.events[-2:], ["cursor-close", "close"])

    def test_legacy_unclaimed_fresh_deferred_pending_and_retryable_are_terminal(self):
        for status in ("fresh", "quota_deferred", "pending", "retryable"):
            with self.subTest(status=status):
                self.update(publish_status=status)
                self.assertTrue(self.reject())
                self.assert_terminal()

    def test_wrong_owner_and_legacy_claim_stealing_are_rolled_back_without_raw_change(self):
        self.claimed()
        before = (self.row(), self.row("cryptonewsapi", 101))
        for token in (None, "stale"):
            with self.subTest(token=token):
                self.assertFalse(self.reject(token=token))
                self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)
        self.assertEqual(self.events.count("rollback"), 2)
        self.assertNotIn("commit", self.events)
        self.assertFalse(any(item[1].startswith("UPDATE") for item in self.events if isinstance(item, tuple)))

    def test_repeated_terminal_call_is_noop_and_row_cannot_be_reclaimed(self):
        self.claimed()
        self.assertTrue(self.reject(token="owner"))
        before = (self.row(), self.row("cryptonewsapi", 101))
        self.assertFalse(self.reject(token="owner"))
        self.assertFalse(self.reject())
        self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)
        self.assertIsNone(self.repository.claim_next(timeout_minutes=30, fresh_start_after=None))

    def test_published_or_external_target_evidence_never_changes_either_row(self):
        for changes in ({"published": 1}, {"wp_post_id": 99}, {"wp_post_url": "https://site.example.invalid/post"},
                        {"wp_post_created_at": "2026-10-28"}, {"publish_status": "post_created"}):
            with self.subTest(changes=changes):
                self.update(published=0, wp_post_id=None, wp_post_url=None, wp_post_created_at=None,
                            publish_status="fresh")
                self.update(**changes)
                before = (self.row(), self.row("cryptonewsapi", 101))
                self.assertFalse(self.reject())
                self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)

    def test_authoritative_raw_id_and_unique_legacy_url_identity(self):
        self.update(news_url="https://news.example.invalid/old-alias")
        self.assertTrue(self.reject())
        self.update(publish_status="fresh", raw_article_id=None, news_url="https://news.example.invalid/current")
        self.assertTrue(self.reject())
        self.update(publish_status="fresh")
        self.database.execute("INSERT INTO cryptonewsapi (id, news_url) VALUES (102, 'https://news.example.invalid/current')")
        self.database.commit()
        before = (self.row(), self.row("cryptonewsapi", 101))
        self.assertFalse(self.reject())
        self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)

    def test_conflicting_raw_id_and_empty_url_cannot_borrow_an_identity(self):
        for raw_id, url in ((999, "https://news.example.invalid/current"), (None, "")):
            with self.subTest(raw_id=raw_id):
                self.update(raw_article_id=raw_id, news_url=url)
                self.assertFalse(self.reject())

    def test_canonical_proof_is_revalidated_in_transaction(self):
        for changes in ({"wp_post_id": 999}, {"raw_article_id": 999}, {"publish_status": "duplicate"}):
            with self.subTest(changes=changes):
                self.update(row_id=27, raw_article_id=17, wp_post_id=37, publish_status="published")
                self.update(row_id=27, **changes)
                before = (self.row(), self.row("cryptonewsapi", 101))
                self.assertFalse(self.reject())
                self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)
        self.update(row_id=27, raw_article_id=17, wp_post_id=None, published=0, publish_status="fresh")
        self.assertFalse(self.reject(decision=replace(self.decision, canonical_wp_id=None)))

    def test_published_or_post_created_canonical_without_id_remains_valid_external_proof(self):
        self.update(row_id=27, wp_post_id=None, published=0, publish_status="post_created")
        self.assertTrue(self.reject(decision=replace(self.decision, canonical_wp_id=None)))

    def test_existing_published_sibling_prevents_historical_raw_mutation(self):
        self.database.execute("""INSERT INTO rich_crpytonews
            (id, raw_article_id, news_url, published, publish_status, wp_post_id)
            VALUES (202, 101, 'https://news.example.invalid/current', 1, 'published', 99)""")
        self.database.commit()
        before = (self.row(), self.row("cryptonewsapi", 101), self.row(row_id=202))
        self.assertFalse(self.reject())
        self.assertEqual((self.row(), self.row("cryptonewsapi", 101), self.row(row_id=202)), before)

    def test_raw_existing_duplicate_disposition_is_preserved(self):
        self.update(table="cryptonewsapi", row_id=101, processing_status="duplicate")
        self.assertTrue(self.reject())
        self.assertEqual(self.row("cryptonewsapi", 101)["processing_status"], "duplicate")

    def test_active_raw_owner_keeps_claim_and_can_complete_after_publication_rejection(self):
        self.update(table="cryptonewsapi", row_id=101, processed=0, processing_status="claimed",
                    processing_claim_token="raw-owner", processing_claimed_at="2026-10-28",
                    processing_last_error="controlled processing diagnostic")
        before = self.row("cryptonewsapi", 101)
        self.assertTrue(self.reject())
        raw = self.row("cryptonewsapi", 101)
        self.assertEqual((raw["processed"], raw["chosen_for_publish"], raw["scheduled_for"]), (1, 0, None))
        for field in ("processing_status", "processing_claim_token", "processing_claimed_at", "processing_last_error"):
            self.assertEqual(raw[field], before[field])
        raw_repository = RawNewsRepository(connect=lambda: Connection(self.database, self.events))
        self.assertTrue(raw_repository.complete(101, "raw-owner"))
        self.assert_terminal()

    def test_late_rich_cas_failure_does_not_update_raw(self):
        self.claimed()
        before = (self.row(), self.row("cryptonewsapi", 101))
        def invalidate_ownership(sql, _params):
            if sql.startswith("UPDATE rich_crpytonews"):
                self.database.execute("UPDATE rich_crpytonews SET publish_claim_token = 'other' WHERE id = 201")
        self.before_execute = invalidate_ownership
        self.assertFalse(self.reject(token="owner"))
        self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)
        self.assertFalse(any(item[1].startswith("UPDATE cryptonewsapi") for item in self.events if isinstance(item, tuple)))
        self.assertIn("rollback", self.events)
        self.assertNotIn("commit", self.events)

    def test_raw_write_failure_rolls_back_rich_disposition_and_closes_transaction(self):
        before = (self.row(), self.row("cryptonewsapi", 101))
        self.database.executescript("""CREATE TRIGGER fail_raw BEFORE UPDATE ON cryptonewsapi
            WHEN NEW.id = 101 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.reject()
        self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)
        self.assertIn("rollback", self.events)
        self.assertNotIn("commit", self.events)
        self.assertEqual(self.events[-2:], ["cursor-close", "close"])

    def test_unapplied_raw_transition_cannot_commit_partial_rich_disposition(self):
        before = (self.row(), self.row("cryptonewsapi", 101))
        self.database.executescript("""CREATE TRIGGER ignore_raw BEFORE UPDATE ON cryptonewsapi
            WHEN NEW.id = 101 BEGIN SELECT RAISE(IGNORE); END;""")
        self.assertFalse(self.reject())
        self.assertEqual((self.row(), self.row("cryptonewsapi", 101)), before)
        self.assertIn("rollback", self.events)
        self.assertNotIn("commit", self.events)

    def test_invalid_or_unconfirmed_decisions_fail_before_database_access(self):
        for changes in ({"reject": False}, {"available": False}, {"mode": "shadow"}, {"stage": "raw"},
                        {"article_id": 102}, {"canonical_raw_id": 101}, {"canonical_rich_id": 201},
                        {"canonical_raw_id": None}, {"canonical_rich_id": None}, {"canonical_wp_id": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.reject(decision=replace(self.decision, **changes))
        self.assertEqual(self.events, [])

    def test_fresh_guard_deferral_restores_first_attempt_permission(self):
        first = self.repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        self.assertIsNotNone(first)
        self.assertEqual(first.attempt, 1)
        self.assertTrue(self.repository.defer_duplicate_guard(201, first.token, creation_is_fresh=True))
        restored = self.row()
        self.assertTrue(is_fresh_publication_state(restored))
        self.assertIsNone(restored["publish_claimed_at"])
        self.assertIsNone(restored["publish_last_error"])
        second = self.repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        self.assertIsNotNone(second)
        self.assertEqual(second.attempt, 1)
        self.assertTrue(is_fresh_publication_state(second.article))

    def test_fresh_deferral_cannot_reset_stale_owner_or_ambiguous_attempt(self):
        self.claimed()
        before = self.row()
        self.assertFalse(self.repository.defer_duplicate_guard(201, "stale", creation_is_fresh=True))
        self.assertEqual(self.row(), before)
        for changes in ({"publish_attempt_count": 2}, {"wp_post_id": 99}, {"wp_post_url": "https://site.example.invalid/post"},
                        {"wp_post_created_at": "2026-10-28"}, {"published": 1}, {"publish_status": "post_created"}):
            with self.subTest(changes=changes):
                self.update(publish_status="claimed", published=0, publish_attempt_count=1,
                            wp_post_id=None, wp_post_url=None, wp_post_created_at=None)
                self.update(**changes)
                before = self.row()
                self.assertFalse(self.repository.defer_duplicate_guard(201, "owner", creation_is_fresh=True))
                self.assertEqual(self.row(), before)

    def test_nonfresh_deferral_uses_manual_reconciliation_without_resetting_attempts(self):
        self.claimed(attempt=2)
        self.assertTrue(self.repository.defer_duplicate_guard(201, "owner", creation_is_fresh=False))
        row = self.row()
        self.assertEqual((row["publish_status"], row["publish_attempt_count"]), ("manual_review", 2))
        self.assertIsNone(row["publish_claim_token"])
        self.assertEqual(row["publish_last_error"], "duplicate_guard_creation_not_fresh")
        self.assertIsNone(self.repository.claim_next(timeout_minutes=30, fresh_start_after=None))


if __name__ == "__main__":
    unittest.main()
