"""Exercise duplicate processing through real terminal-state predicates, offline."""

from contextlib import ExitStack, contextmanager
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gpt_processor
from duplicate_detection.enforcement import DUPLICATE_COUNTER_NAMES, DuplicateDecision, assess_duplicate_enforcement
from operations import jobs
from operations.models import JobStatus
from repositories.raw_news import RawNewsRepository
from tests.test_processing_retry_invocation import SQLiteConnection, SQLiteCursor


class SelectionCursor(SQLiteCursor):
    def fetchall(self):
        rows = self.cursor.fetchall()
        return [dict(row) for row in rows] if self.dictionary else rows


class SelectionConnection(SQLiteConnection):
    def cursor(self, dictionary=False):
        return SelectionCursor(self.database.cursor(), dictionary=dictionary)


class PolicyMemoryRepository:
    def __init__(self, source, candidates):
        self.source = source
        self.candidates = candidates
        self.saved = []

    def load_source_article(self, raw_id):
        return self.source if self.source["id"] == raw_id else None

    def load_enforcement_candidates(self, _raw_id, **_kwargs):
        return self.candidates

    def save_assessments(self, raw_id, assessments, *, audit_context):
        pairs = list(assessments)
        self.saved.append((raw_id, pairs, audit_context))
        return len(pairs)


class DuplicateProcessingEnforcementTests(unittest.TestCase):
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
        self.repository = RawNewsRepository(connect=lambda: SelectionConnection(self.database))
        self.seed(101)

    def seed(self, raw_id):
        self.database.execute("""
            INSERT INTO cryptonewsapi
                (id, news_url, title, source_name, scheduled_for, selected_at, insertDate)
            VALUES (?, ?, ?, 'source', '2020-01-01', '2020-01-01', '2026-10-28')
        """, (raw_id, f"https://news.example.invalid/{raw_id}", f"Article {raw_id}"))
        self.database.commit()

    def row(self, raw_id=101):
        return dict(self.database.execute(
            "SELECT * FROM cryptonewsapi WHERE id = ?", (raw_id,),
        ).fetchone())

    def decision(self, raw_id=101, **changes):
        return replace(DuplicateDecision(
            reject=True, mode="enforce", stage="raw", article_id=raw_id,
            canonical_raw_id=17, canonical_rich_id=27, canonical_wp_id=37,
        ), **changes)

    def counts(self, **values):
        return {**dict.fromkeys(DUPLICATE_COUNTER_NAMES, 0), **values}

    @contextmanager
    def processing(self, *, mode="enforce", durable=True, decision=None, semantic=None):
        with ExitStack() as stack:
            def patched(name, **kwargs):
                return stack.enter_context(patch.object(gpt_processor, name, **kwargs))

            patched("DUPLICATE_ENFORCEMENT_MODE", new=mode)
            patched("PROCESS_DURABLE_CLAIMS_ENABLED", new=durable)
            patched("PROCESS_BATCH_MIN", new=1)
            patched("PROCESS_BATCH_MAX", new=3)
            patched("PIPELINE_FRESH_START_AFTER_UTC_SQL", new=None)
            patched("_count_due_within", return_value=1)
            patched("RawNewsRepository", return_value=self.repository)
            patched("get_db_connection", side_effect=lambda: SelectionConnection(self.database))
            assess = patched("assess_duplicate_enforcement", return_value=(
                decision if decision is not None else DuplicateDecision(mode=mode, article_id=101)
            ))
            shadow = patched("_run_duplicate_shadow_fail_open")
            semantic_call = patched("_run_semantic_shadow_fail_open", return_value=semantic)
            calls = {"assess": assess, "shadow": shadow, "semantic": semantic_call}
            draft = {"title": "Article", "full_text": "<p>Article body.</p>",
                     "schema_jsonld": "{}", "seo_focus": "Bitcoin", "seo_slug": "article",
                     "seo_meta": "Article"}
            calls["enrich"] = patched("enrich_with_search", return_value={})
            calls["video"] = patched("_maybe_video_url", return_value=None)
            calls["rewrite"] = patched("classify_and_rewrite", return_value=(draft, "grok"))
            calls["repair"] = patched("repair_if_needed", side_effect=lambda _r, _e, d, _p: d)
            patched("_clean_final_article_links", side_effect=lambda doc, _raw: doc)
            patched("_capture_quality_stage")
            patched("validate_rewritten_article")
            calls["store"] = patched("store_rich_news")
            calls["mark"] = patched("mark_processed")
            calls["reject"] = stack.enter_context(patch.object(
                self.repository, "reject_duplicate", wraps=self.repository.reject_duplicate,
            ))
            calls["complete"] = stack.enter_context(patch.object(
                self.repository, "complete", wraps=self.repository.complete,
            ))
            calls["fail"] = stack.enter_context(patch.object(
                self.repository, "fail", wraps=self.repository.fail,
            ))
            for level in ("info", "warning", "error", "exception"):
                calls["log_" + level] = stack.enter_context(patch.object(gpt_processor.logging, level))
            yield calls

    def assert_no_generation(self, calls):
        for name in ("enrich", "video", "rewrite", "repair", "store", "mark"):
            calls[name].assert_not_called()

    def assert_terminal_duplicate(self, *, attempts):
        row = self.row()
        self.assertEqual((row["processed"], row["chosen_for_publish"], row["processing_status"],
                          row["processing_attempt_count"]), (1, 0, "duplicate", attempts))
        for field in ("scheduled_for", "processing_claim_token", "processing_claimed_at", "processing_last_error"):
            self.assertIsNone(row[field], field)

    def test_legacy_rejection_is_terminal_nonfailure_before_any_paid_work(self):
        decision = self.decision()
        with self.processing(durable=False, decision=decision) as calls:
            result = gpt_processor.process_news_with_gpt(batch_size=3)
            later = gpt_processor.process_news_with_gpt(batch_size=3)
        self.assertEqual(result, self.counts(attempted=1, succeeded=0, failed=0, duplicates_rejected=1))
        self.assertEqual(later, self.counts(attempted=0, succeeded=0, failed=0, duplicates_rejected=0))
        calls["assess"].assert_called_once_with(self.row_before_rejection(), stage="raw", mode="enforce", semantic_candidates=())
        calls["reject"].assert_called_once_with(101, token=None, decision=decision)
        calls["semantic"].assert_not_called()
        calls["complete"].assert_not_called()
        calls["fail"].assert_not_called()
        self.assert_no_generation(calls)
        self.assert_terminal_duplicate(attempts=0)

    def row_before_rejection(self):
        row = self.row()
        row.update(processed=0, chosen_for_publish=1, processing_status="pending", scheduled_for="2020-01-01")
        return row

    def test_durable_rejection_uses_claim_token_without_complete_or_fail_and_never_retries(self):
        decision = self.decision()
        with self.processing(decision=decision) as calls:
            result = gpt_processor.process_news_with_gpt(batch_size=3)
            later = gpt_processor.process_news_with_gpt(batch_size=3)
        self.assertEqual(result, self.counts(attempted=1, succeeded=0, failed=0, duplicates_rejected=1))
        self.assertEqual(later, self.counts(attempted=0, succeeded=0, failed=0, duplicates_rejected=0))
        self.assertTrue(calls["reject"].call_args.kwargs["token"])
        self.assertEqual(calls["reject"].call_args.kwargs["decision"], decision)
        calls["semantic"].assert_not_called()
        calls["complete"].assert_not_called()
        calls["fail"].assert_not_called()
        self.assert_no_generation(calls)
        self.assert_terminal_duplicate(attempts=1)

    def test_direct_deferred_duplicate_outcome_does_not_write_before_wrapper_ownership_check(self):
        with self.processing(decision=self.decision()) as calls:
            result = gpt_processor.process_one(self.row(), mark_complete=False)
        self.assertIsInstance(result, gpt_processor.ProcessingDuplicate)
        calls["reject"].assert_not_called()
        self.assertEqual(self.row()["processing_status"], "pending")
        self.assert_no_generation(calls)

    def test_only_actual_semantic_candidates_trigger_fresh_deterministic_confirmation(self):
        candidate = SimpleNamespace(candidate_source_article_id=17, native_distance=0.001)
        semantic = SimpleNamespace(candidates=(candidate,))
        with self.processing(semantic=semantic) as calls:
            calls["assess"].side_effect = [DuplicateDecision(mode="enforce", article_id=101), self.decision()]
            result = gpt_processor.process_one(self.row())
        self.assertIsInstance(result, gpt_processor.ProcessingDuplicate)
        self.assertEqual(calls["assess"].call_count, 2)
        self.assertEqual(calls["assess"].call_args.kwargs["semantic_candidates"], (candidate,))
        calls["semantic"].assert_called_once()
        self.assert_no_generation(calls)
        self.assert_terminal_duplicate(attempts=0)

    def test_vector_near_but_unconfirmed_and_unavailable_candidates_continue_once(self):
        for semantic in (None, SimpleNamespace(candidates=()), SimpleNamespace(candidates=(
            SimpleNamespace(candidate_source_article_id=17, native_distance=0.00001),
        ))):
            with self.subTest(semantic=semantic), self.processing(semantic=semantic) as calls:
                self.assertIs(gpt_processor.process_one(self.row(), mark_complete=False), True)
                self.assertEqual(calls["assess"].call_count, 2 if semantic and semantic.candidates else 1)
                calls["enrich"].assert_called_once()
                calls["rewrite"].assert_called_once()
                calls["repair"].assert_called_once()
                calls["store"].assert_called_once()
                calls["reject"].assert_not_called()

    def test_batch_aggregates_only_latest_assessment_once_per_article(self):
        candidate = SimpleNamespace(candidate_source_article_id=17, native_distance=0.001)
        for durable in (False, True):
            with self.subTest(durable=durable), self.processing(
                durable=durable, semantic=SimpleNamespace(candidates=(candidate,)),
            ) as calls:
                calls["assess"].side_effect = [
                    DuplicateDecision(mode="enforce", article_id=101, counters={"related_events_allowed": 1}),
                    DuplicateDecision(mode="enforce", article_id=101, counters={
                        "related_events_allowed": 1, "semantic_candidates_evaluated": 1,
                    }),
                ]
                result = gpt_processor.process_news_with_gpt(batch_size=1)
            self.assertEqual(result, self.counts(
                attempted=1, succeeded=1, failed=0, duplicates_rejected=0,
                related_events_allowed=1, semantic_candidates_evaluated=1,
            ))
            self.database.execute("""UPDATE cryptonewsapi SET processed = 0,
                processing_status = 'pending', processing_claim_token = NULL,
                processing_claimed_at = NULL WHERE id = 101""")
            self.database.commit()

    def test_missing_embedding_counts_fail_open_without_replacing_deterministic_controls(self):
        for status, expected in (("not_ready", 1), ("error", 1), ("no_candidates", 0), ("disabled", 0)):
            with self.subTest(status=status), self.processing(
                durable=False, semantic=SimpleNamespace(status=status, candidates=()),
            ) as calls, patch.object(gpt_processor, "VECTOR_ENABLED", True), patch.object(
                gpt_processor, "SEMANTIC_SHADOW_ENABLED", True,
            ):
                result = gpt_processor.process_news_with_gpt(batch_size=1)
            self.assertEqual(result["fail_open_unavailable_embeddings"], expected)
            calls["assess"].assert_called_once()
            calls["rewrite"].assert_called_once()
            calls["reject"].assert_not_called()

    def test_semantic_audit_failure_with_valid_retrieval_is_not_an_embedding_outage(self):
        candidate = SimpleNamespace(candidate_source_article_id=17, native_distance=0.001)
        semantic = SimpleNamespace(status="error", retrieval_status="retrieved", candidates=(candidate,))
        with self.processing(durable=False, semantic=semantic) as calls, patch.object(
            gpt_processor, "VECTOR_ENABLED", True,
        ), patch.object(gpt_processor, "SEMANTIC_SHADOW_ENABLED", True):
            result = gpt_processor.process_news_with_gpt(batch_size=1)
        self.assertEqual(result["fail_open_unavailable_embeddings"], 0)
        self.assertEqual(calls["assess"].call_count, 2)
        calls["rewrite"].assert_called_once()

    def test_unavailable_audit_cannot_produce_terminal_duplicate(self):
        with self.processing(decision=self.decision(available=False)) as calls:
            self.assertIs(gpt_processor.process_one(self.row(), mark_complete=False), True)
        calls["reject"].assert_not_called()
        calls["rewrite"].assert_called_once()

    def test_assessment_exception_is_sanitized_and_processing_continues(self):
        with self.processing() as calls:
            calls["assess"].side_effect = RuntimeError("private-body secret-token")
            self.assertIs(gpt_processor.process_one(self.row(), mark_complete=False), True)
        calls["rewrite"].assert_called_once()
        rendered = " ".join(str(call) for call in calls["log_warning"].call_args_list)
        self.assertIn("assessment_unavailable", rendered)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("private-body", rendered)
        self.assertNotIn("secret-token", rendered)

    def test_shadow_evaluates_policy_and_off_keeps_original_counter_shape(self):
        for mode in ("shadow", "off"):
            with self.subTest(mode=mode), self.processing(mode=mode, durable=False) as calls:
                self.assertEqual(gpt_processor.process_news_with_gpt(batch_size=1),
                                 self.counts(attempted=1, succeeded=1, failed=0, duplicates_rejected=0)
                                 if mode == "shadow" else {"attempted": 1, "succeeded": 1, "failed": 0})
                self.assertEqual(calls["assess"].call_count, int(mode == "shadow"))
                self.assertEqual(calls["shadow"].call_count, int(mode == "shadow"))
                calls["semantic"].assert_called_once()
                calls["rewrite"].assert_called_once()
                calls["reject"].assert_not_called()

    def test_shadow_and_enforce_share_real_policy_but_only_enforce_stops_processing(self):
        raw = {**self.row(), "event_id": "alpenglow-event-20261028",
               "title": "Solana Alpenglow mainnet activation confirmed",
               "full_text": "Solana validators activate Alpenglow mainnet on October 28, 2026 with 1000 transactions.",
               "publish_date": "2026-10-28 12:00:00"}
        prior = {**raw, "id": 17, "news_url": "https://other.example.invalid/alpenglow",
                 "title": "Solana Confirms Alpenglow Mainnet Activation",
                 "full_text": "Alpenglow mainnet activation on Solana occurs on October 28, 2026 with 1000 transactions. Validators confirm the protocol details.",
                 "source_name": "Other outlet", "processed": 1, "processing_status": "completed",
                 "canonical_rich_id": 27, "canonical_wp_id": 37, "canonical_published": 1,
                 "canonical_publish_status": "published"}
        decisions = []
        for mode in ("shadow", "enforce"):
            repository = PolicyMemoryRepository(raw, [prior])
            def evaluate(selected, **kwargs):
                decision = assess_duplicate_enforcement(selected, repository=repository, **kwargs)
                decisions.append(decision)
                return decision
            with self.subTest(mode=mode), self.processing(
                mode=mode, durable=False, semantic=SimpleNamespace(status="not_ready", candidates=()),
            ) as calls, patch.object(gpt_processor, "VECTOR_ENABLED", True), patch.object(
                gpt_processor, "SEMANTIC_SHADOW_ENABLED", True,
            ):
                calls["assess"].side_effect = evaluate
                counters = {}
                result = gpt_processor.process_one(raw, duplicate_counters=counters)
            self.assertEqual(len(repository.saved), 1)
            self.assertEqual(repository.saved[0][2]["decision"],
                             "would_reject_duplicate" if mode == "shadow" else "reject_duplicate")
            if mode == "shadow":
                self.assertIs(result, True)
                for name in ("enrich", "rewrite", "repair", "store", "mark"):
                    calls[name].assert_called_once()
                calls["reject"].assert_not_called()
                self.assertIn("would_reject_duplicate", str(calls["log_info"].call_args_list))
                self.assertEqual(self.row()["processing_status"], "pending")
                self.assertEqual(counters["shadow_would_reject_duplicates"], 1)
                self.assertEqual(counters["fail_open_unavailable_embeddings"], 1)
                calls["semantic"].assert_called_once()
            else:
                self.assertIsInstance(result, gpt_processor.ProcessingDuplicate)
                self.assert_no_generation(calls)
                self.assert_terminal_duplicate(attempts=0)
                calls["semantic"].assert_not_called()
        self.assertEqual(decisions[0].assessment.assessment_type, decisions[1].assessment.assessment_type)
        self.assertFalse(decisions[0].reject)
        self.assertTrue(decisions[1].reject)

    def test_shadow_semantic_candidates_reach_same_policy_without_any_disposition(self):
        candidate = SimpleNamespace(candidate_source_article_id=17, native_distance=0.001)
        semantic = SimpleNamespace(candidates=(candidate,))
        with self.processing(mode="shadow", durable=False, semantic=semantic) as calls:
            calls["assess"].side_effect = [
                DuplicateDecision(mode="shadow", article_id=101, counters={"related_events_allowed": 1}),
                DuplicateDecision(mode="shadow", article_id=101, counters={
                    "shadow_would_reject_duplicates": 1, "semantic_candidates_evaluated": 1,
                    "cross_source_duplicates_confirmed": 1,
                }),
            ]
            result = gpt_processor.process_news_with_gpt(batch_size=1)
        self.assertEqual(calls["assess"].call_count, 2)
        self.assertEqual(calls["assess"].call_args.kwargs["semantic_candidates"], (candidate,))
        self.assertEqual(calls["assess"].call_args.kwargs["mode"], "shadow")
        self.assertEqual(result, self.counts(
            attempted=1, succeeded=1, failed=0, duplicates_rejected=0,
            shadow_would_reject_duplicates=1, semantic_candidates_evaluated=1,
            cross_source_duplicates_confirmed=1,
        ))
        calls["rewrite"].assert_called_once()
        calls["reject"].assert_not_called()
        calls["complete"].assert_not_called()
        calls["mark"].assert_called_once()

    def test_shadow_ignores_unavailable_or_erroneously_rejecting_decision(self):
        for available in (True, False):
            with self.subTest(available=available), self.processing(
                mode="shadow", decision=self.decision(available=available),
            ) as calls:
                result = gpt_processor.process_news_with_gpt(batch_size=1)
            self.assertEqual((result["succeeded"], result["failed"], result["duplicates_rejected"]), (1, 0, 0))
            calls["rewrite"].assert_called_once()
            calls["reject"].assert_not_called()
            calls["complete"].assert_called_once()
            self.database.execute("""UPDATE cryptonewsapi SET processed = 0, processing_status = 'pending'
                WHERE id = 101""")
            self.database.commit()

    def test_lost_durable_ownership_is_failure_without_false_duplicate_success(self):
        with self.processing(decision=self.decision()) as calls:
            def stolen(raw_id, *, token, decision):
                self.database.execute("UPDATE cryptonewsapi SET processing_claim_token = 'new-owner' WHERE id = ?", (raw_id,))
                self.database.commit()
                return False
            calls["reject"].side_effect = stolen
            result = gpt_processor.process_news_with_gpt(batch_size=3)
        self.assertEqual(result, self.counts(attempted=1, succeeded=0, failed=1, duplicates_rejected=0))
        self.assertEqual((self.row()["processed"], self.row()["processing_status"],
                          self.row()["processing_claim_token"]), (0, "claimed", "new-owner"))
        calls["complete"].assert_not_called()
        self.assert_no_generation(calls)

    def test_terminal_rejection_keeps_operation_successful(self):
        with self.processing(decision=self.decision()) as calls:
            result = jobs._execute_process(jobs.JobRequest())
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertEqual(result.counts["failed"], 0)
        self.assertEqual(result.counts["duplicates_rejected"], 1)
        self.assert_no_generation(calls)

    def test_duplicate_disposition_database_error_never_reports_handled_duplicate(self):
        with self.processing(durable=False, decision=self.decision()) as calls:
            calls["reject"].side_effect = RuntimeError("private-body secret-token")
            self.assertIs(gpt_processor.process_one(self.row()), False)
        self.assertEqual(self.row()["processing_status"], "pending")
        rendered = " ".join(str(call) for call in calls["log_error"].call_args_list)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("private-body", rendered)
        self.assert_no_generation(calls)

    def test_repository_rejects_wrong_stale_and_repeated_owner_tokens(self):
        claim = self.repository.claim_next(timeout_minutes=30, lookahead_minutes=None, fresh_start_after=None)
        self.assertIsNotNone(claim)
        self.assertFalse(self.repository.reject_duplicate(101, token="wrong", decision=self.decision()))
        self.assertFalse(self.repository.reject_duplicate(101, decision=self.decision()))
        self.assertEqual(self.row()["processing_claim_token"], claim.token)
        self.assertTrue(self.repository.reject_duplicate(101, token=claim.token, decision=self.decision()))
        self.assertFalse(self.repository.reject_duplicate(101, token=claim.token, decision=self.decision()))
        self.assertFalse(self.repository.complete(101, claim.token))
        self.assertFalse(self.repository.fail(101, claim.token, "stale retry"))
        self.assertIsNone(self.repository.claim_next(timeout_minutes=30, lookahead_minutes=None, fresh_start_after=None))
        self.assert_terminal_duplicate(attempts=1)

    def test_legacy_rejection_accepts_only_eligible_unclaimed_rows(self):
        for status in (None, "pending", "retryable", "claimed", "completed", "duplicate"):
            with self.subTest(status=status):
                self.database.execute("""UPDATE cryptonewsapi SET processed = 0, chosen_for_publish = 1,
                    processing_status = ?, processing_claim_token = NULL, processing_claimed_at = NULL
                    WHERE id = 101""", (status,))
                self.database.commit()
                self.assertEqual(self.repository.reject_duplicate(101, decision=self.decision()),
                                 status in (None, "pending", "retryable"))
        self.database.execute("UPDATE cryptonewsapi SET processing_status = 'pending', processing_claim_token = 'owner' WHERE id = 101")
        self.database.commit()
        self.assertFalse(self.repository.reject_duplicate(101, decision=self.decision()))

    def test_repository_requires_confirmed_matching_available_raw_decision(self):
        for decision in (self.decision(reject=False), self.decision(available=False),
                         self.decision(mode="shadow"), self.decision(stage="pre_publish"),
                         self.decision(raw_id=102)):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.repository.reject_duplicate(101, decision=decision)
        self.assertEqual(self.row()["processed"], 0)


if __name__ == "__main__":
    unittest.main()
