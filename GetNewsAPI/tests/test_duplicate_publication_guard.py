"""The same-event check owns the full claim/media/WordPress-create interval."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import config
import mysql.connector

from duplicate_detection.enforcement import assess_duplicate_enforcement
from duplicate_detection.publication import (
    PublicationDuplicateGuard, PublicationDuplicateRejected, PublicationDuplicateUnavailable,
)
from operations.locking import JobLockError
from publishing.models import PublicationArticle, PublicationContext, PublicationResult
from publishing.service import PublishingService
from publishing.wordpress import publisher
from repositories.publication import PublicationClaim


def raw_article(raw_id):
    return {
        "id": raw_id, "event_id": "solana-alpenglow-20261028", "news_id": f"source-{raw_id}",
        "news_url": f"https://source.example/{raw_id}", "source_name": "Independent outlet",
        "title": "Solana Alpenglow mainnet activation",
        "full_text": (
            "Solana validators activate Alpenglow mainnet on October 28, 2026 with 1000 transactions."
            if raw_id == 10 else
            "Alpenglow mainnet activation on Solana occurs on October 28, 2026 with 1000 transactions. Validators confirm the protocol details."
        ),
        "publish_date": "2026-10-28 12:00:00", "processed": 1, "processing_status": "completed",
        "chosen_for_publish": 1,
    }


class SharedStore:
    def __init__(self):
        self.raw = {raw_id: raw_article(raw_id) for raw_id in (10, 20)}
        self.rich = {
            raw_id + 100: {
                "id": raw_id + 100, "raw_article_id": raw_id, "news_url": row["news_url"],
                "title": row["title"], "full_text": "<p>A rewritten article.</p>", "category": "General",
                "published": 0, "publish_status": "fresh", "publish_attempt_count": 0,
                "publish_claim_token": None, "wp_post_id": None, "wp_post_url": None,
                "wp_post_created_at": None, "wp_media_id": None,
            } for raw_id, row in self.raw.items()
        }
        self.events, self.audits, self.media_calls, self.requests = [], [], [], []
        self.mutex = threading.Lock()
        self.owner = None
        self.require_mutex = True

    @contextmanager
    def lock(self):
        acquired = self.mutex.acquire(timeout=5)
        if acquired:
            self.owner = threading.get_ident()
            self.events.append(("lock", self.owner))
        try:
            yield SimpleNamespace(acquired=acquired, name="synthetic-duplicate-publication")
        finally:
            if acquired:
                self.events.append(("unlock", self.owner))
                self.owner = None
                self.mutex.release()

    def assert_owned(self):
        if self.require_mutex and self.owner != threading.get_ident():
            raise AssertionError("claim/image/create/persist escaped the duplicate mutex")

    def load_source_article(self, raw_id):
        return deepcopy(self.raw.get(raw_id))

    def load_enforcement_candidates(self, article_id, **_kwargs):
        result = []
        for row in self.rich.values():
            raw_id = row["raw_article_id"]
            if raw_id == article_id:
                continue
            result.append({
                **deepcopy(self.raw[raw_id]), "canonical_rich_id": row["id"],
                "canonical_wp_id": row.get("wp_post_id"), "canonical_wp_url": row.get("wp_post_url"),
                "canonical_published": row["published"], "canonical_publish_status": row["publish_status"],
                "canonical_publish_attempt_count": row["publish_attempt_count"],
            })
        return result

    def save_assessments(self, article_id, assessments, *, audit_context):
        pairs = list(assessments)
        self.audits.append((article_id, pairs, deepcopy(audit_context)))
        return len(pairs)

    def guard(self, raw_id, mode="enforce"):
        return PublicationDuplicateGuard(raw_id, mode=mode, repository=self, lock_factory=self.lock)

    def image(self, row):
        self.assert_owned()
        self.events.append(("image", row["id"]))
        self.media_calls.append(row["id"])
        return row["id"] + 500, {"provider": "pexels", "asset_key": f"pexels:{row['id']}"}


class ClaimRepository:
    """Use independently claimed rich rows and committed, shared canonical state."""
    def __init__(self, store, rich_id):
        self.store, self.rich_id = store, rich_id
        self.claims, self.failures, self.rejections, self.manual = [], [], [], []
        self.released = []

    def claim_next(self, **kwargs):
        self.store.assert_owned()
        self.store.events.append(("claim", self.rich_id))
        row = self.store.rich[self.rich_id]
        if self.rich_id in kwargs.get("exclude_rich_article_ids", ()) or row["publish_status"] not in {"fresh", "pending", "retryable"}:
            return None
        original = deepcopy(row)
        token = str(self.rich_id) * 10
        row.update(publish_status="claimed", publish_attempt_count=row["publish_attempt_count"] + 1,
                   publish_claim_token=token)
        self.claims.append(token)
        return PublicationClaim(token, original, row["publish_attempt_count"])

    def owned(self, rich_id, token):
        self.store.assert_owned()
        return rich_id == self.rich_id and self.store.rich[rich_id]["publish_claim_token"] == token

    def save_identity(self, rich_id, token, raw_id, key, **_kwargs):
        if not self.owned(rich_id, token):
            return False
        self.store.rich[rich_id].update(raw_article_id=raw_id, publication_key=key)
        return True

    def save_media(self, rich_id, token, media_id, metadata):
        if not self.owned(rich_id, token):
            return False
        self.store.rich[rich_id].update(wp_media_id=media_id, wp_media_metadata_json=deepcopy(metadata))
        return True

    def save_external_post(self, rich_id, token, post_id, post_url):
        if not self.owned(rich_id, token):
            return False
        self.store.events.append(("persist", rich_id))
        self.store.rich[rich_id].update(wp_post_id=post_id, wp_post_url=post_url, publish_status="post_created")
        return True

    def complete(self, rich_id, token):
        if not self.owned(rich_id, token):
            return False
        self.store.rich[rich_id].update(published=1, publish_status="published", publish_claim_token=None)
        return True

    def reject_duplicate(self, rich_id, raw_id, decision, *, token):
        if not self.owned(rich_id, token):
            return False
        if self.store.rich[rich_id]["raw_article_id"] != raw_id:
            return False
        self.rejections.append(decision)
        self.store.rich[rich_id].update(publish_status="duplicate", publish_claim_token=None)
        self.store.raw[self.store.rich[rich_id]["raw_article_id"]]["chosen_for_publish"] = 0
        return True

    def require_manual_reconciliation(self, rich_id, token, error):
        if not self.owned(rich_id, token):
            return False
        self.manual.append(error)
        self.store.rich[rich_id].update(publish_status="manual_review", publish_claim_token=None)
        return True

    def fail(self, rich_id, token, error):
        self.failures.append(error)
        self.store.rich[rich_id].update(publish_status="retryable", publish_claim_token=None)
        return True

    def release_interrupted(self, rich_id, token):
        self.released.append((rich_id, token))
        return True


class PublicationGuardTests(unittest.TestCase):
    def adapter(self):
        adapter = publisher.WordPressPublisher()
        adapter.reconcile = Mock(return_value=None)
        adapter.find_external_media = Mock(return_value=None)
        adapter.persist_external_media_identity = Mock()
        return adapter

    def service(self, store, rich_id, *, guard_factory=None, adapter=None, mode="enforce"):
        repo = ClaimRepository(store, rich_id)
        service = PublishingService(repo, adapter or self.adapter(), store.image, duplicate_mode=mode,
                                    duplicate_guard_factory=guard_factory or (lambda raw_id: store.guard(raw_id, mode)))
        return service, repo

    @contextmanager
    def wordpress(self, store):
        def post(url, *, json, **_kwargs):
            store.assert_owned()
            store.events.append(("post", threading.get_ident()))
            store.requests.append((url, deepcopy(json)))
            response = Mock(status_code=201)
            response.json.return_value = {"id": 900 + len(store.requests), "link": f"https://coincourier.example/{900 + len(store.requests)}"}
            return response
        with ExitStack() as stack:
            stack.enter_context(patch.object(publisher.session, "post", side_effect=post))
            stack.enter_context(patch.object(publisher, "ensure_category", return_value=1))
            stack.enter_context(patch.object(publisher, "link_markets", side_effect=lambda value: value))
            stack.enter_context(patch.object(publisher.WordPressPublisher, "_validate_yoast"))
            stack.enter_context(patch.object(publisher.WordPressPublisher, "_write_yoast"))
            usage = stack.enter_context(patch.object(publisher, "record_image_usage"))
            yield usage

    def test_concurrent_cross_source_same_event_workers_create_one_post_and_one_media(self):
        store = SharedStore()
        store.raw[10].update(event_id="upstream-a-activation", source_name="Synthetic source A")
        store.raw[20].update(event_id="upstream-b-activation", source_name="Synthetic source B")
        # Both raw checks pass before either rewrite has created a rich row.
        rich_rows, store.rich = store.rich, {}
        for raw in store.raw.values():
            self.assertFalse(assess_duplicate_enforcement(raw, stage="raw", mode="enforce", repository=store).reject)
        store.rich = rich_rows
        workers = [self.service(store, rich_id) for rich_id in (110, 120)]
        start = threading.Barrier(2)
        def run(worker):
            start.wait(timeout=5)
            return worker[0].publish_due(1)
        with self.wordpress(store) as usage, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, workers))
        self.assertEqual(len(store.requests), 1)
        self.assertEqual(len(store.media_calls), 1)
        self.assertEqual(sum(result["succeeded"] for result in results), 1)
        self.assertEqual(sum(result["failed"] for result in results), 0)
        self.assertEqual(sum(result.get("duplicates_rejected", 0) for result in results), 1)
        self.assertEqual(sum(result.get("same_event_blocks", 0) for result in results), 1)
        self.assertEqual(sorted(row["publish_status"] for row in store.rich.values()), ["duplicate", "published"])
        self.assertEqual(sum(len(repo.rejections) for _service, repo in workers), 1)
        self.assertFalse(any(repo.failures for _service, repo in workers))
        usage.assert_called_once()
        self.assertEqual(store.requests[0][1]["featured_media"], store.media_calls[0] + 500)
        phases = [event[0] for event in store.events]
        self.assertLess(phases.index("lock"), phases.index("claim"))
        self.assertLess(phases.index("persist"), phases.index("unlock"))
        rejected = next(repo.rejections[0] for _service, repo in workers if repo.rejections)
        self.assertFalse(rejected.assessment.same_event_id)
        self.assertEqual(rejected.assessment.event_identity_source, "structured_cross_source")

    def test_later_material_numeric_update_creates_second_post(self):
        store = SharedStore()
        store.raw[20]["full_text"] = store.raw[20]["full_text"].replace("1000 transactions", "2000 transactions")
        with self.wordpress(store):
            first, _ = self.service(store, 110)
            second, repo = self.service(store, 120)
            self.assertEqual(first.publish_due(1)["succeeded"], 1)
            self.assertEqual(second.publish_due(1)["succeeded"], 1)
        self.assertEqual(len(store.requests), 2)
        self.assertEqual(len(store.media_calls), 2)
        self.assertEqual(repo.rejections, [])

    def test_pre_create_recheck_detects_latest_prior_before_http(self):
        store = SharedStore()
        def image(row):
            result = store.image(row)
            store.rich[120].update(publish_status="published", published=1, wp_post_id=800,
                                   wp_post_url="https://coincourier.example/800")
            return result
        service, repo = self.service(store, 110)
        service.image_preparer = image
        with self.wordpress(store):
            result = service.publish_due(1)
        self.assertEqual(store.requests, [])
        self.assertEqual(len(store.media_calls), 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result.get("duplicates_rejected"), 1)
        self.assertEqual(repo.rejections[0].canonical_wp_id, 800)

    def test_existing_own_wp_reconciliation_does_not_enter_fresh_guard(self):
        store = SharedStore()
        store.rich[110].update(wp_post_id=777, wp_post_url="https://coincourier.example/777")
        adapter = self.adapter()
        adapter.reconcile.return_value = PublicationResult(True, external_id=777, reconciled=True)
        service, repo = self.service(store, 110, adapter=adapter)
        with self.wordpress(store):
            result = service.publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(store.requests, [])
        self.assertEqual(store.media_calls, [])
        self.assertEqual(store.rich[110]["wp_post_id"], 777)
        self.assertEqual(repo.rejections, [])

    def test_lock_busy_leaves_fresh_row_unclaimed_without_paid_image(self):
        store = SharedStore()
        @contextmanager
        def busy():
            yield SimpleNamespace(acquired=False, name="synthetic-busy")
        service, repo = self.service(store, 110, guard_factory=lambda raw_id: PublicationDuplicateGuard(
            raw_id, mode="enforce", repository=store, lock_factory=busy))
        with self.wordpress(store):
            result = service.publish_due(1)
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(repo.claims, [])
        self.assertEqual(store.rich[110]["publish_status"], "fresh")
        self.assertEqual(store.media_calls, [])
        self.assertEqual(store.requests, [])

    def test_lock_acquisition_errors_defer_before_claim_or_image(self):
        for error_type in (mysql.connector.Error, JobLockError):
            with self.subTest(error_type=error_type.__name__):
                store = SharedStore()
                @contextmanager
                def unavailable():
                    raise error_type("synthetic sensitive acquisition failure")
                    yield  # The context fails during acquisition, before yielding.
                service, repo = self.service(store, 110, guard_factory=lambda raw_id: PublicationDuplicateGuard(
                    raw_id, mode="enforce", repository=store, lock_factory=unavailable))
                with self.wordpress(store), self.assertLogs("publishing.service", level="WARNING") as logs:
                    result = service.publish_due(1)
                self.assertEqual(result["attempted"], 0)
                self.assertEqual(result["failed"], 0)
                self.assertEqual(result["duplicate_guard_deferred"], 1)
                self.assertEqual(repo.claims, [])
                self.assertEqual(store.rich[110]["publish_status"], "fresh")
                self.assertEqual(store.media_calls, [])
                self.assertEqual(store.requests, [])
                self.assertIn("duplicate_publication_lock_unavailable", " ".join(logs.output))
                self.assertNotIn("sensitive", " ".join(logs.output))

    def test_publisher_body_database_error_is_failure_without_lock_mislabel(self):
        store = SharedStore()
        adapter = self.adapter()
        adapter.publish = Mock(side_effect=mysql.connector.Error("synthetic publisher SQL failure"))
        service, repo = self.service(store, 110, adapter=adapter)
        with self.wordpress(store):
            result = service.publish_due(1)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["duplicate_guard_deferred"], 0)
        self.assertEqual(len(repo.failures), 1)
        self.assertNotIn("duplicate_publication_lock_unavailable", repo.failures[0])
        self.assertIsNone(store.owner)

    def test_prior_unknown_post_outcome_enters_manual_review_without_retry_loop(self):
        store = SharedStore()
        store.rich[120].update(publish_status="retryable", publish_attempt_count=1)
        service, repo = self.service(store, 110)
        with self.wordpress(store):
            first, second = service.publish_due(1), service.publish_due(1)
        self.assertEqual(first["attempted"], 1)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(store.rich[110]["publish_status"], "manual_review")
        self.assertEqual(len(repo.manual), 1)
        self.assertFalse(repo.failures)
        self.assertEqual(store.media_calls, [])
        self.assertEqual(store.requests, [])

    def test_direct_wordpress_callback_rejection_prevents_http_create(self):
        store = SharedStore()
        store.rich[120].update(publish_status="published", published=1, wp_post_id=800)
        guard = store.guard(10)
        article = PublicationArticle("Solana Alpenglow activation", "<p>Article body.</p>")
        context = PublicationContext(datetime.now(timezone.utc), creation_is_fresh=True,
                                     pre_create_duplicate_guard=guard.check)
        with guard.hold(), self.wordpress(store):
            with self.assertRaises(PublicationDuplicateRejected):
                self.adapter().publish(article, None, context)
        self.assertEqual(store.requests, [])

    def test_missing_embedding_service_does_not_affect_final_deterministic_guard(self):
        store = SharedStore()
        store.rich[120].update(publish_status="published", published=1, wp_post_id=800)
        service, _repo = self.service(store, 110)
        with self.wordpress(store), patch("embeddings.provider.OpenAIEmbeddingProvider", side_effect=AssertionError("must not embed")) as embedding:
            result = service.publish_due(1)
        self.assertEqual(result.get("duplicates_rejected"), 1)
        self.assertEqual(store.requests, [])
        self.assertEqual(store.media_calls, [])
        embedding.assert_not_called()

    def test_legacy_batch_rejects_before_image_or_post_without_failure(self):
        store = SharedStore()
        store.rich[120].update(publish_status="published", published=1, wp_post_id=800)
        repository = Mock()
        repository.get_external_post_state.return_value = deepcopy(store.rich[110])
        repository.reject_duplicate.return_value = True
        adapter = self.adapter()
        with (
            self.wordpress(store),
            patch.object(publisher.mysql.connector, "connect", return_value=Mock()),
            patch.object(publisher, "_get_lock", return_value=True),
            patch.object(publisher, "_release_lock", return_value=True),
            patch.object(publisher, "_count_due_now", return_value=1),
            patch.object(publisher, "fetch_unpublished", return_value=[deepcopy(store.rich[110])]),
            patch.object(publisher, "PUBLISH_DURABLE_STATE_ENABLED", False),
            patch.object(publisher, "DUPLICATE_ENFORCEMENT_MODE", "enforce"),
            patch.object(publisher, "PublicationRepository", return_value=repository),
            patch.object(publisher, "WordPressPublisher", return_value=adapter),
            patch.object(publisher, "PublicationDuplicateGuard", side_effect=lambda raw_id, **_kwargs: store.guard(raw_id)),
            patch.object(publisher, "upload_image") as upload,
            patch.object(publisher, "mark_news_as_published") as mark_published,
        ):
            result = publisher.publish_news_to_wp()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["duplicates_rejected"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(store.requests, [])
        upload.assert_not_called()
        mark_published.assert_not_called()
        rich_id, raw_id, decision = repository.reject_duplicate.call_args.args
        self.assertEqual((rich_id, raw_id, decision.canonical_wp_id), (110, 10, 800))
        repository.begin_legacy_creation.assert_not_called()

    def test_shadow_pre_publish_logs_would_reject_but_prepares_media_and_creates_post(self):
        store = SharedStore()
        store.require_mutex = False
        store.rich[120].update(publish_status="published", published=1, wp_post_id=800)
        lock = Mock(side_effect=AssertionError("shadow must not acquire an enforcement mutex"))
        factory = lambda raw_id: PublicationDuplicateGuard(raw_id, mode="shadow", repository=store, lock_factory=lock)
        service, repo = self.service(store, 110, mode="shadow", guard_factory=factory)
        with self.wordpress(store), self.assertLogs(level="INFO") as logs:
            result = service.publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["duplicates_rejected"], 0)
        self.assertEqual(result["duplicate_guard_deferred"], 0)
        self.assertEqual(result["shadow_would_reject_duplicates"], 1)
        self.assertEqual(len(store.media_calls), 1)
        self.assertEqual(len(store.requests), 1)
        self.assertEqual(repo.rejections, [])
        self.assertEqual(repo.manual, [])
        self.assertEqual(store.rich[110]["publish_status"], "published")
        self.assertEqual(len(store.audits), 2)  # Before media, then immediately before CREATE.
        self.assertTrue(all(audit[2]["decision"] == "would_reject_duplicate" for audit in store.audits))
        self.assertIn("mode=shadow", " ".join(logs.output))
        self.assertIn("stage=pre_publish", " ".join(logs.output))
        self.assertIn("decision=would_reject_duplicate", " ".join(logs.output))
        lock.assert_not_called()

    def test_shadow_assessment_errors_are_sanitized_and_never_block_publication(self):
        store = SharedStore()
        store.require_mutex = False
        service, repo = self.service(store, 110, mode="shadow")
        with self.wordpress(store), patch(
            "duplicate_detection.publication.assess_duplicate_enforcement",
            side_effect=RuntimeError("sensitive assessment connection details"),
        ) as assess, self.assertLogs("duplicate_detection.publication", level="WARNING") as logs:
            result = service.publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["duplicates_rejected"], 0)
        self.assertEqual(result["duplicate_guard_deferred"], 0)
        self.assertEqual(result["shadow_would_reject_duplicates"], 0)
        self.assertEqual(len(store.media_calls), 1)
        self.assertEqual(len(store.requests), 1)
        self.assertEqual(assess.call_count, 2)
        self.assertEqual(repo.rejections, [])
        self.assertIn("decision=would_continue", " ".join(logs.output))
        self.assertIn("error_type=RuntimeError", " ".join(logs.output))
        self.assertNotIn("sensitive", " ".join(logs.output))

    def test_shadow_and_enforce_guards_share_engine_and_classification(self):
        store = SharedStore()
        store.rich[120].update(publish_status="published", published=1, wp_post_id=800)
        decisions = {}
        with patch("duplicate_detection.publication.assess_duplicate_enforcement",
                   wraps=assess_duplicate_enforcement) as assess:
            for mode in ("shadow", "enforce"):
                guard = store.guard(10, mode)
                with guard.hold():
                    if mode == "enforce":
                        with self.assertRaises(PublicationDuplicateRejected):
                            guard.check()
                    else:
                        guard.check()
                decisions[mode] = guard.decision
        self.assertEqual([call.kwargs["mode"] for call in assess.call_args_list], ["shadow", "enforce"])
        self.assertTrue(all(call.kwargs["stage"] == "pre_publish" for call in assess.call_args_list))
        self.assertEqual(decisions["shadow"].assessment, decisions["enforce"].assessment)
        self.assertFalse(decisions["shadow"].reject)
        self.assertTrue(decisions["enforce"].reject)
        self.assertEqual(decisions["shadow"].audit["decision"], "would_reject_duplicate")
        self.assertEqual(decisions["enforce"].audit["decision"], "reject_duplicate")

    def test_legacy_shadow_runs_policy_twice_and_never_applies_duplicate_disposition(self):
        store = SharedStore()
        store.require_mutex = False
        store.rich[120].update(publish_status="published", published=1, wp_post_id=800)
        repository = Mock()
        repository.get_external_post_state.return_value = deepcopy(store.rich[110])
        adapter = self.adapter()
        with (
            self.wordpress(store),
            patch.object(publisher.mysql.connector, "connect", return_value=Mock()),
            patch.object(publisher, "_get_lock", return_value=True),
            patch.object(publisher, "_release_lock", return_value=True),
            patch.object(publisher, "_count_due_now", return_value=1),
            patch.object(publisher, "fetch_unpublished", return_value=[deepcopy(store.rich[110])]),
            patch.object(publisher, "PUBLISH_DURABLE_STATE_ENABLED", False),
            patch.object(publisher, "DUPLICATE_ENFORCEMENT_MODE", "shadow"),
            patch.object(publisher, "PublicationRepository", return_value=repository),
            patch.object(publisher, "WordPressPublisher", return_value=adapter),
            patch.object(publisher, "PublicationDuplicateGuard", side_effect=lambda raw_id, **kwargs: store.guard(raw_id, kwargs["mode"])),
            patch.object(publisher, "upload_image", return_value=(610, {"provider": "pexels"})) as upload,
            patch.object(publisher, "mark_news_as_published") as mark_published,
            self.assertLogs(level="INFO") as logs,
        ):
            result = publisher.publish_news_to_wp()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["duplicates_rejected"], 0)
        self.assertEqual(result["shadow_would_reject_duplicates"], 1)
        self.assertEqual(len(store.requests), 1)
        self.assertEqual(len(store.audits), 2)
        upload.assert_called_once()
        mark_published.assert_called_once()
        repository.reject_duplicate.assert_not_called()
        repository.defer_legacy_manual_reconciliation.assert_not_called()
        self.assertIn("decision=would_reject_duplicate", " ".join(logs.output))


class PublicationGuardLivenessTests(unittest.TestCase):
    def test_off_hold_and_check_perform_no_database_or_assessment_io(self):
        connect = Mock(side_effect=AssertionError("off must not connect"))
        lock = Mock(side_effect=AssertionError("off must not lock"))
        repository = Mock()
        guard = PublicationDuplicateGuard(10, mode="off", repository=repository,
                                          connection_factory=connect, lock_factory=lock)
        with patch("duplicate_detection.publication.assess_duplicate_enforcement") as assess:
            guard.check()
            with guard.hold() as held:
                self.assertIs(held, guard)
                guard.check()
            guard.check()
        connect.assert_not_called()
        lock.assert_not_called()
        assess.assert_not_called()
        self.assertEqual(repository.mock_calls, [])

    def probe(self, *, owner=(71, 71), failure=None):
        """Exercise the real advisory-lock path with SQL-only connection doubles."""
        connection, cursor = Mock(), Mock()
        connection.cursor.return_value = cursor
        last_sql = []
        def execute(sql, _params):
            last_sql[:] = [sql]
            if "IS_USED_LOCK" in sql and failure == "execute":
                raise mysql.connector.Error("synthetic sensitive connection failure")
        def fetchone():
            sql = last_sql[0]
            if "DATABASE()" in sql:
                return ("coincourier_guard_test",)
            if "IS_USED_LOCK" in sql:
                if failure == "fetchone":
                    raise mysql.connector.Error("synthetic sensitive connection failure")
                return owner
            return (1,)
        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = fetchone
        if failure == "cursor":
            calls = 0
            def make_cursor():
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise mysql.connector.Error("synthetic sensitive connection failure")
                return cursor
            connection.cursor.side_effect = make_cursor
        if failure == "close":
            closed = 0
            def close_cursor():
                nonlocal closed
                closed += 1
                if closed == 3:
                    raise mysql.connector.Error("synthetic sensitive connection failure")
            cursor.close.side_effect = close_cursor
        return connection, cursor

    def test_lost_real_connection_lock_ownership_prevents_assessment(self):
        for owner in (None, (None, 71), (72, 71)):
            with self.subTest(owner=owner):
                connection, cursor = self.probe(owner=owner)
                guard = PublicationDuplicateGuard(10, mode="enforce", connection_factory=lambda: connection)
                with patch.dict(config.DB_CONFIG, {"database": "coincourier_guard_test"}), patch(
                    "duplicate_detection.publication.assess_duplicate_enforcement"
                ) as assess:
                    with guard.hold():
                        with self.assertRaises(PublicationDuplicateUnavailable) as caught:
                            guard.check()
                self.assertEqual(caught.exception.code, "duplicate_publication_lock_lost")
                assess.assert_not_called()
                self.assertTrue(any("IS_USED_LOCK" in call.args[0] for call in cursor.execute.call_args_list))
                connection.close.assert_called_once()

    def test_connection_errors_are_controlled_lock_loss_without_assessment(self):
        for stage in ("cursor", "execute", "fetchone", "close"):
            with self.subTest(stage=stage):
                connection, _cursor = self.probe(failure=stage)
                guard = PublicationDuplicateGuard(10, mode="enforce", connection_factory=lambda: connection)
                with patch.dict(config.DB_CONFIG, {"database": "coincourier_guard_test"}), patch(
                    "duplicate_detection.publication.assess_duplicate_enforcement"
                ) as assess:
                    with guard.hold():
                        with self.assertRaises(PublicationDuplicateUnavailable) as caught:
                            guard.check()
                self.assertEqual(caught.exception.code, "duplicate_publication_lock_lost")
                self.assertNotIn("sensitive", str(caught.exception))
                assess.assert_not_called()
                connection.close.assert_called_once()

    def test_invalid_mode_fails_before_database_access(self):
        connect = Mock()
        for mode in ("", "invalid", "enforced"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                PublicationDuplicateGuard(10, mode=mode, connection_factory=connect)
        connect.assert_not_called()

    def test_yielded_body_database_error_keeps_its_original_type_and_releases_lock(self):
        store = SharedStore()
        guard = store.guard(10)
        error = mysql.connector.Error("synthetic body error")
        with self.assertRaises(mysql.connector.Error) as caught:
            with guard.hold():
                raise error
        self.assertIs(caught.exception, error)
        self.assertIsNone(store.owner)


if __name__ == "__main__":
    unittest.main()
