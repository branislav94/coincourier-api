"""Exercise invocation-local processing retries with real selection and validation."""

from contextlib import ExitStack, contextmanager
import sqlite3
import unittest
from unittest.mock import patch

import gpt_processor
from duplicate_detection.enforcement import DUPLICATE_COUNTER_NAMES, DuplicateDecision
from operations import jobs
from operations.models import JobStatus
from repositories.raw_news import RawNewsRepository


class SQLiteCursor:
    """Translate only MySQL time/locking syntax; SQLite evaluates the predicates."""

    def __init__(self, cursor, *, dictionary):
        self.cursor = cursor
        self.dictionary = dictionary

    def execute(self, sql, params=()):
        sql = sql.replace(
            "TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP())",
            "datetime('now', '-' || ? || ' minutes')",
        ).replace(
            "TIMESTAMPADD(MINUTE, %s, UTC_TIMESTAMP())",
            "datetime('now', '+' || ? || ' minutes')",
        )
        sql = sql.replace("UTC_TIMESTAMP()", "CURRENT_TIMESTAMP")
        sql = sql.replace("FOR UPDATE", "").replace("%s", "?")
        self.cursor.execute(sql, params)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None and self.dictionary else row

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
        return SQLiteCursor(self.database.cursor(), dictionary=dictionary)

    def commit(self):
        self.database.commit()

    def rollback(self):
        self.database.rollback()

    def close(self):
        # Keep the test's in-memory database alive between repository connections.
        pass


class ProcessingRetryInvocationTests(unittest.TestCase):
    def counts(self, *, attempted, succeeded, failed):
        return {"attempted": attempted, "succeeded": succeeded, "failed": failed,
                "duplicates_rejected": 0, **dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0)}

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        self.database.execute("""
            CREATE TABLE cryptonewsapi (
                id INTEGER PRIMARY KEY, news_url TEXT, title TEXT, source_name TEXT,
                processed INTEGER DEFAULT 0, chosen_for_publish INTEGER DEFAULT 1,
                scheduled_for TEXT, selected_at TEXT, insertDate TEXT,
                processing_status TEXT DEFAULT 'pending', processing_claim_token TEXT,
                processing_claimed_at TEXT, processing_attempt_count INTEGER DEFAULT 0,
                processing_last_error TEXT
            )
        """)
        self.repository = RawNewsRepository(connect=lambda: SQLiteConnection(self.database))
        self.short_ids = set()

    def seed(self, raw_id, *, scheduled="2020-01-01 00:00:00", selected=None,
             inserted="2026-10-14 00:00:00", chosen=1):
        self.database.execute(
            """INSERT INTO cryptonewsapi
               (id, news_url, title, source_name, scheduled_for, selected_at,
                insertDate, chosen_for_publish) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (raw_id, f"https://example.invalid/{raw_id}", f"Article {raw_id}",
             "test", scheduled, selected or scheduled, inserted, chosen),
        )
        self.database.commit()

    def row(self, raw_id):
        return dict(self.database.execute(
            "SELECT * FROM cryptonewsapi WHERE id = ?", (raw_id,)
        ).fetchone())

    def rewrite(self, raw, *_args):
        words = 449 if raw["id"] in self.short_ids else 450
        return {
            "title": raw["title"], "full_text": "<p>" + " ".join(["word"] * words) + "</p>",
            "category": "Bitcoin", "hashtags": "#Bitcoin", "sentiment": "neutral",
            "seo_focus": "Bitcoin", "seo_slug": f"article-{raw['id']}",
            "seo_meta": "A test article about Bitcoin.", "image_alt": "Bitcoin",
            "schema_jsonld": "{}",
        }, "grok"

    @contextmanager
    def processing(self, *, due=1, minimum=3, maximum=12, cutoff=None):
        with ExitStack() as stack:
            def patched(name, **kwargs):
                return stack.enter_context(patch.object(gpt_processor, name, **kwargs))

            patched("PROCESS_DURABLE_CLAIMS_ENABLED", new=True)
            patched("DUPLICATE_ENFORCEMENT_MODE", new="shadow")
            patched("PROCESS_BATCH_MIN", new=minimum)
            patched("PROCESS_BATCH_MAX", new=maximum)
            patched("PIPELINE_FRESH_START_AFTER_UTC_SQL", new=cutoff)
            patched("RawNewsRepository", return_value=self.repository)
            patched("_count_due_within", return_value=due)
            duplicate = patched("_run_duplicate_shadow_fail_open")
            policy = patched("assess_duplicate_enforcement", side_effect=lambda raw, **_kwargs: DuplicateDecision(
                mode="shadow", article_id=raw["id"], counters=dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0),
            ))
            semantic = patched("_run_semantic_shadow_fail_open")
            enrich = patched("enrich_with_search", return_value={})
            patched("_maybe_video_url", return_value=None)
            rewrite = patched("classify_and_rewrite", side_effect=self.rewrite)
            repair = patched("repair_if_needed", side_effect=lambda _raw, _extra, draft, _provider: draft)
            store = patched("store_rich_news")
            mark = patched("mark_processed")
            claim = stack.enter_context(patch.object(
                self.repository, "claim_next", wraps=self.repository.claim_next,
            ))
            for level in ("info", "warning", "error", "exception"):
                stack.enter_context(patch.object(gpt_processor.logging, level))
            yield {
                "duplicate": duplicate, "policy": policy, "semantic": semantic, "enrich": enrich,
                "rewrite": rewrite, "repair": repair, "store": store, "mark": mark,
                "claim": claim,
            }

    def test_validation_failure_leaves_slots_for_other_ids_and_retries_next_invocation(self):
        for raw_id in (18160, 18161, 18162):
            self.seed(raw_id)
        self.short_ids.add(18160)

        # A due count below the minimum must still allow three distinct attempts.
        with self.processing(due=1) as calls:
            result = gpt_processor.process_news_with_gpt()
        self.assertEqual(result, self.counts(attempted=3, succeeded=2, failed=1))
        for boundary in ("duplicate", "policy", "semantic", "enrich", "rewrite", "repair"):
            self.assertEqual([call.args[0]["id"] for call in calls[boundary].call_args_list],
                             [18160, 18161, 18162])
        self.assertEqual([call.args[1]["id"] for call in calls["store"].call_args_list],
                         [18161, 18162])
        calls["mark"].assert_not_called()
        self.assertEqual([call.kwargs["exclude_raw_article_ids"]
                          for call in calls["claim"].call_args_list],
                         [(), (18160,), (18160, 18161)])
        failed = self.row(18160)
        self.assertEqual((failed["processed"], failed["processing_status"],
                          failed["processing_attempt_count"]), (0, "retryable", 1))
        self.assertIsNone(failed["processing_claim_token"])
        self.assertIsNone(failed["processing_claimed_at"])
        self.assertIn("ArticleValidationError", failed["processing_last_error"])
        for raw_id in (18161, 18162):
            self.assertEqual((self.row(raw_id)["processed"], self.row(raw_id)["processing_status"],
                              self.row(raw_id)["processing_attempt_count"]), (1, "completed", 1))

        self.short_ids.clear()
        with self.processing() as later_calls:
            later = gpt_processor.process_news_with_gpt()
        self.assertEqual(later, self.counts(attempted=1, succeeded=1, failed=0))
        self.assertEqual([call.args[0]["id"] for call in later_calls["rewrite"].call_args_list], [18160])
        self.assertEqual(later_calls["claim"].call_args_list[0].kwargs["exclude_raw_article_ids"], ())
        self.assertEqual((self.row(18160)["processed"], self.row(18160)["processing_attempt_count"]), (1, 2))
        self.assertIsNone(self.row(18160)["processing_last_error"])

    def test_one_failed_article_cannot_fill_minimum_batch_or_hide_pipeline_failure(self):
        self.seed(18160)
        self.short_ids.add(18160)
        with self.processing() as calls, patch(
            "publish_to_wp.publish_news_to_wp",
            return_value={"attempted": 3, "succeeded": 3, "failed": 0},
        ) as publish:
            result = jobs._execute_pipeline(jobs.JobRequest())
        self.assertEqual((result.status, result.stage, result.reason_type),
                         (JobStatus.FAILED, "process", "ArticleFailures"))
        self.assertEqual(result.counts, {
            **{f"process_{name}": count for name, count in self.counts(attempted=1, succeeded=0, failed=1).items()},
            "publish_attempted": 3, "publish_succeeded": 3, "publish_failed": 0,
        })
        self.assertEqual(calls["rewrite"].call_count, 1)
        self.assertEqual(calls["claim"].call_count, 2)  # One attempt, then no eligible row.
        self.assertEqual(self.row(18160)["processing_attempt_count"], 1)
        self.assertEqual(self.row(18160)["processing_status"], "retryable")
        publish.assert_called_once()

    def test_multiple_failures_each_consume_only_one_actual_attempt(self):
        for raw_id in (1, 2, 3):
            self.seed(raw_id)
        self.short_ids.update((1, 2, 3))
        with self.processing() as calls:
            result = gpt_processor.process_news_with_gpt(batch_size=8)
        self.assertEqual(result, self.counts(attempted=3, succeeded=0, failed=3))
        self.assertEqual([call.args[0]["id"] for call in calls["rewrite"].call_args_list], [1, 2, 3])
        self.assertEqual(calls["claim"].call_count, 4)
        self.assertTrue(all(self.row(raw_id)["processing_attempt_count"] == 1 for raw_id in (1, 2, 3)))

    def test_explicit_limit_and_processing_order_are_unchanged(self):
        self.seed(30, scheduled="2020-01-02 00:00:00")
        self.seed(20, selected="2020-01-01 00:00:02")
        self.seed(10, selected="2020-01-01 00:00:01")
        self.seed(9, selected="2020-01-01 00:00:01")
        with self.processing(minimum=1, maximum=1) as calls:
            result = gpt_processor.process_news_with_gpt(batch_size=2)
        self.assertEqual(result, self.counts(attempted=2, succeeded=2, failed=0))
        self.assertEqual([call.args[0]["id"] for call in calls["rewrite"].call_args_list], [9, 10])
        self.assertTrue(all(call.kwargs["lookahead_minutes"] is None
                            for call in calls["claim"].call_args_list))
        self.assertEqual(self.row(20)["processing_attempt_count"], 0)
        self.assertEqual(self.row(30)["processing_attempt_count"], 0)

    def test_auto_maximum_and_selection_filters_are_unchanged(self):
        for raw_id in (1, 2, 3):
            self.seed(raw_id)
        self.seed(0, inserted="2019-01-01 00:00:00")
        self.seed(4, scheduled="2999-01-01 00:00:00")
        self.seed(5, chosen=0)
        with self.processing(due=99, minimum=1, maximum=2,
                             cutoff="2026-01-01 00:00:00") as calls:
            result = gpt_processor.process_news_with_gpt()
        self.assertEqual(result, self.counts(attempted=2, succeeded=2, failed=0))
        self.assertEqual([call.args[0]["id"] for call in calls["rewrite"].call_args_list], [1, 2])
        self.assertTrue(all(call.kwargs["lookahead_minutes"] == gpt_processor.PROCESS_LOOKAHEAD_MINUTES
                            for call in calls["claim"].call_args_list))
        self.assertTrue(all(self.row(raw_id)["processing_attempt_count"] == 0 for raw_id in (0, 3, 4, 5)))

    def test_zero_and_negative_explicit_limits_do_not_claim(self):
        self.seed(1)
        for limit in (0, -1):
            with self.subTest(limit=limit), self.processing() as calls:
                self.assertEqual(gpt_processor.process_news_with_gpt(batch_size=limit),
                                 self.counts(attempted=0, succeeded=0, failed=0))
                calls["claim"].assert_not_called()
                calls["rewrite"].assert_not_called()

    def test_exception_and_lost_completion_ownership_cannot_repeat_in_same_batch(self):
        for failure in ("exception", "ownership"):
            with self.subTest(failure=failure):
                self.database.execute("DELETE FROM cryptonewsapi")
                self.seed(1)
                self.seed(2)
                real_complete = self.repository.complete
                def complete(raw_id, token):
                    return False if raw_id == 1 else real_complete(raw_id, token)
                def process(raw, **_kwargs):
                    if raw["id"] == 1 and failure == "exception":
                        raise RuntimeError("local processing failure")
                    return True
                with self.processing(), patch.object(gpt_processor, "process_one", side_effect=process) as work:
                    with patch.object(self.repository, "complete", side_effect=complete):
                        result = gpt_processor.process_news_with_gpt(batch_size=5)
                self.assertEqual(result, self.counts(attempted=2, succeeded=1, failed=1))
                self.assertEqual([call.args[0]["id"] for call in work.call_args_list], [1, 2])
                self.assertEqual(self.row(1)["processing_attempt_count"], 1)
                self.assertEqual(self.row(1)["processing_status"], "retryable")


if __name__ == "__main__":
    unittest.main()
