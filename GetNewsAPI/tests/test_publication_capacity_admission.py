"""Offline coverage for advisory scheduling and paid-generation capacity guards."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from GetNewsAPI.tests.environment_isolation import install_test_environment

install_test_environment()

import fetcher
import gpt_processor
from duplicate_detection.enforcement import DUPLICATE_COUNTER_NAMES, DuplicateDecision
from repositories.raw_news import RawNewsRepository


class SQLiteCursor:
    """Translate MySQL clock syntax; SQLite evaluates eligibility predicates."""

    def __init__(self, cursor, dictionary=False):
        self.cursor = cursor
        self.dictionary = dictionary

    def execute(self, sql, params=()):
        sql = sql.replace(
            "DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s HOUR)",
            "datetime('now', '-' || ? || ' hours')",
        ).replace(
            "TIMESTAMPADD(HOUR, -%s, UTC_TIMESTAMP())",
            "datetime('now', '-' || ? || ' hours')",
        ).replace(
            "TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP())",
            "datetime('now', '-' || ? || ' minutes')",
        ).replace("UTC_TIMESTAMP()", "CURRENT_TIMESTAMP").replace("UTC_DATE()", "CURRENT_DATE")
        self.cursor.execute(sql.replace("FOR UPDATE", "").replace("%s", "?"), params)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None and self.dictionary else row

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()] if self.dictionary else self.cursor.fetchall()

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def close(self):
        self.cursor.close()


class SQLiteConnection:
    def __init__(self, database):
        self.database = database

    def start_transaction(self):
        self.database.execute("BEGIN")

    def cursor(self, dictionary=False):
        return SQLiteCursor(self.database.cursor(), dictionary)

    def commit(self):
        self.database.commit()

    def rollback(self):
        self.database.rollback()

    def close(self):
        pass


class AdmissionCapacityTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        self.database.executescript("""
            CREATE TABLE cryptonewsapi (
                id INTEGER PRIMARY KEY, news_url TEXT, chosen_for_publish INTEGER DEFAULT 1,
                publish_date TEXT, selected_at TEXT, scheduled_for TEXT, insertDate TEXT,
                processed INTEGER DEFAULT 0, is_breaking INTEGER DEFAULT 0,
                processing_status TEXT DEFAULT 'pending',
                processing_claimed_at TEXT, processing_claim_token TEXT,
                processing_attempt_count INTEGER DEFAULT 0, processing_last_error TEXT
            );
            CREATE TABLE rich_crpytonews (
                id INTEGER PRIMARY KEY, news_url TEXT, published INTEGER DEFAULT 0,
                wp_post_id INTEGER, publish_status TEXT DEFAULT 'fresh'
            );
            CREATE TABLE publication_capacity_reservations (rich_article_id INTEGER PRIMARY KEY, utc_date TEXT);
        """)
        self.now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        self.enterContext(patch.object(gpt_processor, "DUPLICATE_ENFORCEMENT_MODE", "shadow"))

    def processing_counts(self, **values):
        return {
            "attempted": 0, "succeeded": 0, "failed": 0, "duplicates_rejected": 0,
            **dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0), **values,
        }

    def seed(self, article_id, *, generated=False, source_hours=1, selected_hours=25,
             published=0, wp_post_id=None, status="fresh", reserved=False, reservation_date=None):
        source_date = self.now - timedelta(hours=source_hours)
        selected_date = self.now - timedelta(hours=selected_hours)
        self.database.execute(
            """INSERT INTO cryptonewsapi
               (id, news_url, publish_date, selected_at, scheduled_for, insertDate)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (article_id, f"https://example.invalid/{article_id}", str(source_date),
             str(selected_date), str(selected_date), str(selected_date)),
        )
        if generated:
            self.database.execute(
                "INSERT INTO rich_crpytonews VALUES (?, ?, ?, ?, ?)",
                (article_id, f"https://example.invalid/{article_id}", published, wp_post_id, status),
            )
        if reserved:
            self.database.execute("INSERT INTO publication_capacity_reservations VALUES (?, ?)",
                                  (article_id, reservation_date or str(self.now.date())))
        self.database.commit()

    @contextmanager
    def capacity(self, remaining=1):
        snapshot = SimpleNamespace(remaining=remaining, utc_date=self.now.date(), limit=30)
        connection = lambda: SQLiteConnection(self.database)
        with ExitStack() as stack:
            for module in (fetcher, gpt_processor):
                stack.enter_context(patch.object(module, "PUBLISH_DAILY_HARD_CAP_ENABLED", True))
                stack.enter_context(patch.object(module, "CapacityRepository", return_value=SimpleNamespace(
                    snapshot=lambda: snapshot,
                )))
                stack.enter_context(patch.object(module, "get_db_connection", side_effect=connection))
            yield snapshot

    def test_generation_prioritizes_selected_breaking_news_with_one_slot(self):
        self.seed(1)
        self.seed(2)
        self.database.execute('UPDATE cryptonewsapi SET is_breaking = 1 WHERE id = 2')
        self.database.commit()
        with (
            self.capacity(remaining=1),
            patch.object(gpt_processor, 'PROCESS_DURABLE_CLAIMS_ENABLED', False),
            patch.object(gpt_processor, 'PIPELINE_FRESH_START_AFTER_UTC_SQL', None),
            patch.object(gpt_processor, 'process_one', return_value=True) as process,
        ):
            result = gpt_processor.process_news_with_gpt(batch_size=2)
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(process.call_args.args[0]['id'], 2)

    def test_durable_generation_claim_prioritizes_selected_breaking_news(self):
        self.seed(1)
        self.seed(2)
        self.database.execute('UPDATE cryptonewsapi SET is_breaking = 1 WHERE id = 2')
        self.database.commit()
        repository = RawNewsRepository(connect=lambda: SQLiteConnection(self.database))
        claim = repository.claim_next(
            timeout_minutes=90, lookahead_minutes=None,
            fresh_start_after=None, source_max_age_hours=36,
        )
        self.assertEqual(claim.article['id'], 2)

    def test_pending_queue_includes_prior_day_and_excludes_existing_reservations_and_posts(self):
        self.seed(1)  # Previous-day selection still commits one future CREATE.
        self.seed(2, generated=True)
        self.seed(3, generated=True, reserved=True)
        self.seed(4, generated=True, wp_post_id=44)
        self.seed(5, generated=True, published=1)
        self.seed(6, generated=True, status="manual_review")
        self.seed(7, source_hours=37)
        with self.capacity(remaining=10):
            self.assertEqual(fetcher._pending_publication_commitments(), 2)
            self.assertEqual(gpt_processor._generation_capacity_remaining(), 9)

    def test_full_capacity_skips_processing_before_due_count_claims_or_paid_work(self):
        self.seed(1)
        with self.capacity(remaining=0), patch.object(gpt_processor, "_count_due_within") as due, \
             patch.object(gpt_processor, "RawNewsRepository") as claims, \
             patch.object(gpt_processor, "process_one") as process:
            for mode in ("off", "shadow", "enforce"):
                with self.subTest(mode=mode), patch.object(
                    gpt_processor, "DUPLICATE_ENFORCEMENT_MODE", mode,
                ):
                    for _ in range(2):
                        result = gpt_processor.process_news_with_gpt()
                        expected = (
                            self.processing_counts(quota_deferred=1)
                            if mode in {"shadow", "enforce"} else
                            {"attempted": 0, "succeeded": 0, "failed": 0, "quota_deferred": 1}
                        )
                        self.assertEqual(result, expected)
            due.assert_not_called()
            claims.assert_not_called()
            process.assert_not_called()
        self.assertEqual(self.database.execute(
            "SELECT processed, processing_attempt_count FROM cryptonewsapi"
        ).fetchone()[:], (0, 0))

    def test_previous_day_unattempted_reservation_still_commits_todays_capacity(self):
        self.seed(1, generated=True, reserved=True,
                  reservation_date=str((self.now - timedelta(days=1)).date()))
        with self.capacity(remaining=1):
            self.assertEqual(fetcher._pending_publication_commitments(), 1)
            self.assertEqual(gpt_processor._generation_capacity_remaining(), 0)

    def test_missing_capacity_schema_defers_without_claim_or_generation(self):
        with patch.object(gpt_processor, "PUBLISH_DAILY_HARD_CAP_ENABLED", True), \
             patch.object(gpt_processor, "CapacityRepository", side_effect=RuntimeError("missing schema")), \
             patch.object(gpt_processor, "RawNewsRepository") as claims, \
             patch.object(gpt_processor, "process_one") as process:
            self.assertEqual(gpt_processor.process_news_with_gpt(batch_size=9)["quota_deferred"], 1)
            claims.assert_not_called()
            process.assert_not_called()

    def test_generated_queue_prevents_repeated_direct_generation(self):
        self.seed(1, generated=True)
        self.seed(2)
        with self.capacity(remaining=1), patch.object(gpt_processor, "RawNewsRepository") as claims, \
             patch.object(gpt_processor, "process_one") as process:
            self.assertEqual(gpt_processor.process_news_with_gpt(batch_size=9)["quota_deferred"], 1)
            claims.assert_not_called()
            process.assert_not_called()

    def test_auto_and_explicit_batches_are_bounded_by_available_capacity(self):
        for batch_size in (None, 9):
            with self.subTest(batch_size=batch_size), self.capacity(remaining=1), \
                 patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", True), \
                 patch.object(gpt_processor, "_count_due_within", return_value=20), \
                 patch.object(gpt_processor, "_process_news_with_durable_claims", return_value={}) as process:
                gpt_processor.process_news_with_gpt(batch_size=batch_size)
                self.assertEqual(process.call_args.args, (1,))

    def test_capacity_refresh_stops_before_next_durable_claim(self):
        article = {"id": 1, "news_url": "https://example.invalid/1"}
        repository = Mock()
        repository.claim_next.return_value = SimpleNamespace(article=article, token="token", attempt=1, recovered=False)
        repository.complete.return_value = True
        with patch.object(gpt_processor, "PUBLISH_DAILY_HARD_CAP_ENABLED", True), \
             patch.object(gpt_processor, "RawNewsRepository", return_value=repository), \
             patch.object(gpt_processor, "_generation_capacity_remaining", side_effect=[1, 0]), \
             patch.object(gpt_processor, "process_one", return_value=True) as process:
            result = gpt_processor._process_news_with_durable_claims(2, use_lookahead_filter=False)
        self.assertEqual(result, self.processing_counts(attempted=1, succeeded=1, quota_deferred=1))
        self.assertEqual(repository.claim_next.call_count, 1)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(repository.claim_next.call_args.kwargs["source_max_age_hours"], 36)

    def test_stale_sources_remain_unprocessed_and_unclaimed(self):
        self.seed(1, source_hours=37)
        self.seed(2, source_hours=1)
        repository = RawNewsRepository(connect=lambda: SQLiteConnection(self.database))
        claim = repository.claim_next(timeout_minutes=90, lookahead_minutes=None,
                                      fresh_start_after=None, source_max_age_hours=36)
        self.assertEqual(claim.article["id"], 2)
        self.assertEqual(self.database.execute(
            "SELECT processed, processing_status, processing_attempt_count FROM cryptonewsapi WHERE id=1"
        ).fetchone()[:], (0, "pending", 0))

    def test_legacy_processing_rechecks_capacity_before_each_paid_attempt(self):
        self.seed(1)
        self.seed(2)
        with self.capacity(remaining=2), \
             patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", False), \
             patch.object(gpt_processor, "_generation_capacity_remaining", side_effect=[2, 1, 0]), \
             patch.object(gpt_processor, "process_one", return_value=True) as process:
            result = gpt_processor.process_news_with_gpt(batch_size=2)
        self.assertEqual(result, self.processing_counts(attempted=1, succeeded=1, quota_deferred=1))
        self.assertEqual(process.call_count, 1)

    def test_legacy_processing_filters_stale_backlog_before_paid_work(self):
        self.seed(1, source_hours=37)
        self.seed(2, source_hours=1)
        with self.capacity(remaining=2), \
             patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", False), \
             patch.object(gpt_processor, "process_one", return_value=True) as process:
            result = gpt_processor.process_news_with_gpt(batch_size=2)
        self.assertEqual(result, self.processing_counts(attempted=1, succeeded=1))
        self.assertEqual(process.call_args.args[0]["id"], 2)

    @contextmanager
    def combined_processing(self, *, durable, decision):
        repository = RawNewsRepository(connect=lambda: SQLiteConnection(self.database))
        draft = {
            "title": "Synthetic article", "full_text": "<p>Synthetic article body.</p>",
            "schema_jsonld": "{}", "seo_focus": "Bitcoin", "seo_slug": "synthetic",
            "seo_meta": "Synthetic article description.",
        }
        with ExitStack() as stack:
            def patched(name, **kwargs):
                return stack.enter_context(patch.object(gpt_processor, name, **kwargs))

            patched("DUPLICATE_ENFORCEMENT_MODE", new=decision.mode)
            patched("PROCESS_DURABLE_CLAIMS_ENABLED", new=durable)
            patched("PIPELINE_FRESH_START_AFTER_UTC_SQL", new=None)
            patched("RawNewsRepository", return_value=repository)
            patched("assess_duplicate_enforcement", return_value=decision)
            patched("_run_duplicate_shadow_fail_open")
            patched("_run_semantic_shadow_fail_open", return_value=None)
            calls = {"enrich": patched("enrich_with_search", return_value={})}
            patched("_maybe_video_url", return_value=None)
            calls["rewrite"] = patched("classify_and_rewrite", return_value=(draft, "grok"))
            patched("repair_if_needed", side_effect=lambda _raw, _extra, doc, _provider: doc)
            patched("_clean_final_article_links", side_effect=lambda doc, _raw: doc)
            patched("_capture_quality_stage")
            patched("validate_rewritten_article")
            calls["store"] = patched("store_rich_news")
            calls["mark"] = patched("mark_processed")
            for name in ("reject_duplicate", "complete", "fail"):
                calls[name] = stack.enter_context(patch.object(
                    repository, name, wraps=getattr(repository, name),
                ))
            yield calls

    def test_capacity_admission_preserves_terminal_duplicates_and_claim_tokens(self):
        for article_id, durable in enumerate((False, True), start=1):
            self.seed(article_id)
            decision = DuplicateDecision(
                reject=True, mode="enforce", stage="raw", article_id=article_id,
                canonical_raw_id=17, canonical_wp_id=37, counters={"same_event_blocks": 1},
            )
            with self.subTest(durable=durable), self.capacity(remaining=1), \
                 self.combined_processing(durable=durable, decision=decision) as calls:
                result = gpt_processor.process_news_with_gpt(batch_size=1)
            self.assertEqual(result, self.processing_counts(
                attempted=1, duplicates_rejected=1, same_event_blocks=1,
            ))
            arguments = calls["reject_duplicate"].call_args
            self.assertEqual(arguments.args, (article_id,))
            self.assertEqual(arguments.kwargs["decision"], decision)
            if durable:
                self.assertTrue(arguments.kwargs["token"])
            else:
                self.assertIsNone(arguments.kwargs["token"])
            row = self.database.execute("""
                SELECT processed, chosen_for_publish, processing_status,
                       processing_attempt_count, processing_claim_token
                FROM cryptonewsapi WHERE id = ?
            """, (article_id,)).fetchone()
            self.assertEqual(tuple(row), (1, 0, "duplicate", int(durable), None))
            for name in ("enrich", "rewrite", "store", "mark", "complete", "fail"):
                calls[name].assert_not_called()

    def test_capacity_admission_preserves_shadow_counters_and_generation(self):
        for durable in (False, True):
            self.database.execute("DELETE FROM cryptonewsapi")
            self.seed(1)
            decision = DuplicateDecision(
                mode="shadow", article_id=1,
                counters={"shadow_would_reject_duplicates": 1},
            )
            with self.subTest(durable=durable), self.capacity(remaining=1), \
                 self.combined_processing(durable=durable, decision=decision) as calls:
                result = gpt_processor.process_news_with_gpt(batch_size=1)
            self.assertEqual(result, self.processing_counts(
                attempted=1, succeeded=1, shadow_would_reject_duplicates=1,
            ))
            for name in ("enrich", "rewrite", "store"):
                calls[name].assert_called_once()
            calls["reject_duplicate"].assert_not_called()
            calls["fail"].assert_not_called()
            self.assertEqual(calls["complete"].call_count, int(durable))
            self.assertEqual(calls["mark"].call_count, int(not durable))

    def test_capacity_admission_cannot_report_duplicate_after_claim_ownership_loss(self):
        self.seed(1)
        decision = DuplicateDecision(reject=True, mode="enforce", article_id=1)
        with self.capacity(remaining=1), \
             self.combined_processing(durable=True, decision=decision) as calls:
            def stolen(raw_id, *, token, decision):
                self.database.execute(
                    "UPDATE cryptonewsapi SET processing_claim_token = 'new-owner' WHERE id = ?",
                    (raw_id,),
                )
                self.database.commit()
                return False

            calls["reject_duplicate"].side_effect = stolen
            result = gpt_processor.process_news_with_gpt(batch_size=1)
        self.assertEqual(result, self.processing_counts(attempted=1, failed=1))
        row = self.database.execute("""
            SELECT processed, processing_status, processing_claim_token
            FROM cryptonewsapi WHERE id = 1
        """).fetchone()
        self.assertEqual(tuple(row), (0, "claimed", "new-owner"))
        for name in ("enrich", "rewrite", "store", "mark", "complete"):
            calls[name].assert_not_called()

    def test_disabled_processing_does_not_query_capacity_or_filter_source_age(self):
        with patch.object(gpt_processor, "PUBLISH_DAILY_HARD_CAP_ENABLED", False), \
             patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", True), \
             patch.object(gpt_processor, "CapacityRepository") as capacity, \
             patch.object(gpt_processor, "_process_news_with_durable_claims", return_value={}) as process:
            gpt_processor.process_news_with_gpt(batch_size=9)
            capacity.assert_not_called()
            self.assertEqual(process.call_args.args, (9,))


class BreakingAdmissionTests(unittest.TestCase):
    def plan(self, *, enabled, remaining, selected=29, pending=0):
        connection = Mock()
        connection.cursor.return_value.fetchall.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]
        snapshot = SimpleNamespace(remaining=remaining, utc_date="2026-10-15", limit=30)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(fetcher, "PUBLISH_DAILY_HARD_CAP_ENABLED", enabled))
        capacity = stack.enter_context(patch.object(fetcher, "CapacityRepository", return_value=SimpleNamespace(
            snapshot=lambda: snapshot,
        )))
        database = stack.enter_context(patch.object(fetcher, "get_db_connection", return_value=connection))
        stack.enter_context(patch.object(fetcher, "_pending_publication_commitments", return_value=pending))
        stack.enter_context(patch.object(fetcher, "DAILY_TARGET", 30))
        stack.enter_context(patch.object(fetcher, "_today_published_counts", return_value=(selected, 0)))
        assign = stack.enter_context(patch.object(fetcher, "_assign_schedule"))
        fetcher._plan_today_schedule()
        return capacity, database, assign

    def test_breaking_cannot_select_at_full_capacity(self):
        _, database, assign = self.plan(enabled=True, remaining=0)
        database.assert_not_called()
        assign.assert_not_called()

    def test_breaking_gets_last_slot_without_exceeding_remaining_capacity(self):
        _, _, assign = self.plan(enabled=True, remaining=1)
        assign.assert_called_once()
        self.assertEqual(assign.call_args.args[0], [1])

    def test_previous_day_selected_queue_uses_remaining_admission_capacity(self):
        _, database, assign = self.plan(enabled=True, remaining=1, selected=0, pending=1)
        database.assert_not_called()
        assign.assert_not_called()

    def test_enabled_breaking_also_respects_existing_daily_selection_target(self):
        _, database, assign = self.plan(enabled=True, remaining=30, selected=30)
        database.assert_not_called()
        assign.assert_not_called()

    def test_disabled_breaking_retains_existing_behavior(self):
        capacity, _, assign = self.plan(enabled=False, remaining=0, selected=30)
        capacity.assert_not_called()
        assign.assert_called_once()
        self.assertEqual(assign.call_args.args[0], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
