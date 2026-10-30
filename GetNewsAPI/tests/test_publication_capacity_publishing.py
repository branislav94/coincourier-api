"""Offline publication-capacity orchestration, HTTP and legacy regressions.

The shared, thread-safe capacity fake exercises callers; transaction-level
MariaDB concurrency is covered separately by the capacity integration suite.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
from GetNewsAPI.tests.environment_isolation import install_test_environment

install_test_environment()

from duplicate_detection.enforcement import DuplicateDecision
from duplicate_detection.publication import PublicationDuplicateRejected
from publishing.capacity import CAPACITY_UNAVAILABLE, QUOTA_EXHAUSTED, STALE_BACKLOG
from publishing.models import PublicationArticle, PublicationContext
from publishing.service import PublishingService, build_publication_key
from publishing.wordpress import publisher
from repositories.publication import PublicationClaim, PublicationRepository
from repositories.publication_capacity import CapacityAlreadyAttempted, CapacityUnavailable
from GetNewsAPI.tests.test_publication_capacity_admission import SQLiteConnection


ORIGIN = "https://wordpress.example.invalid"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def article_row(article_id=1, **overrides):
    return {
        "id": article_id, "raw_article_id": article_id + 100,
        "news_url": f"https://source.example.invalid/{article_id}",
        "title": f"Article {article_id}", "full_text": "<p>Bitcoin news.</p>",
        "category": "Markets", "hashtags": "", "seo_focus": "Bitcoin news",
        "seo_meta": "A factual Bitcoin news description.", "seo_slug": f"article-{article_id}",
        "published": 0, "publish_status": "fresh", "publish_attempt_count": 0,
        "publish_claim_token": None, "wp_post_id": None, "wp_post_url": None,
        "wp_post_created_at": None, "wp_media_id": None,
        "source_publish_date": NOW - timedelta(hours=1),
        "selected_at": NOW - timedelta(hours=1), "is_breaking": 0,
        **overrides,
    }


class SharedCapacity:
    def __init__(self, used=0, now=NOW):
        self.used = used
        self.now = now
        self.rows = {}
        self.lock = threading.RLock()
        self.unavailable = False
        self.force_reserve_none = False
        self.reserve_calls = 0
        self.begin_calls = 0

    def _check(self):
        if self.unavailable:
            raise CapacityUnavailable("synthetic missing schema")

    def _remaining(self):
        return max(0, 30 - self.used - sum(row["date"] == self.now.date() for row in self.rows.values()))

    def snapshot(self):
        with self.lock:
            self._check()
            return SimpleNamespace(remaining=self._remaining(), utc_date=self.now.date(), limit=30)

    def reserve(self, article_id, key):
        with self.lock:
            self._check()
            self.reserve_calls += 1
            old = self.rows.get(article_id)
            if old and old["started"]:
                raise CapacityAlreadyAttempted("synthetic prior CREATE")
            if self.force_reserve_none:
                return None
            if old is None or old["date"] != self.now.date():
                if not self._remaining():
                    return None
                self.rows[article_id] = {"date": self.now.date(), "key": key, "started": False, "wp_post_id": None}
            return SimpleNamespace(**self.rows[article_id])

    def begin_create(self, article_id, key):
        with self.lock:
            self.begin_calls += 1
            if self.reserve(article_id, key) is None:
                return None
            self.rows[article_id]["started"] = True
            self.rows[article_id]["publication_at"] = self.now
            return self.now

    def record_created(self, article_id, key, post_id):
        with self.lock:
            self._check()
            row = self.rows[article_id]
            if row["key"] != key or not row["started"]:
                raise CapacityUnavailable("synthetic identity mismatch")
            row["wp_post_id"] = post_id

    def unattempted_today(self, article_id):
        with self.lock:
            row = self.rows.get(article_id)
            return bool(row and row["date"] == self.now.date() and not row["started"])


class ApplicationRepository:
    """Model the application's claim ownership around real service/adapter calls."""

    def __init__(self, rows, capacity=None):
        self.rows = {row["id"]: deepcopy(row) for row in rows}
        self.capacity = capacity
        self.lock = threading.RLock()
        self.claim_options = []
        self.failures = []

    def claim_next(self, *, exclude_rich_article_ids=(), existing_posts_only=False,
                   reserved_posts_allowed=False, **options):
        with self.lock:
            self.claim_options.append(dict(options, existing_posts_only=existing_posts_only,
                                           reserved_posts_allowed=reserved_posts_allowed))
            candidates = [row for row in self.rows.values() if not row["published"]
                          and row["id"] not in exclude_rich_article_ids
                          and row["publish_status"] in ("fresh", "quota_deferred", "pending", "retryable")]
            if existing_posts_only:
                candidates = [row for row in candidates if row["wp_post_id"] is not None or (
                    reserved_posts_allowed and self.capacity and self.capacity.unattempted_today(row["id"])
                )]
            if not candidates:
                return None
            candidates.sort(key=lambda row: (row["wp_post_id"] is None, not row["is_breaking"], row["id"]))
            row = candidates[0]
            snapshot = deepcopy(row)
            row.update(publish_status="claimed", publish_claim_token=f"claim-{row['id']}")
            row["publish_attempt_count"] += 1
            return PublicationClaim(row["publish_claim_token"], snapshot, row["publish_attempt_count"])

    def save_identity(self, article_id, _token, raw_id, key, **_expectations):
        self.rows[article_id].update(raw_article_id=raw_id, publication_key=key)
        return True

    def save_external_post(self, article_id, _token, post_id, post_url):
        self.rows[article_id].update(wp_post_id=post_id, wp_post_url=post_url,
                                     wp_post_created_at=NOW, publish_status="post_created")
        return True

    def complete(self, article_id, _token):
        self.rows[article_id].update(published=1, publish_status="published", publish_claim_token=None)
        return True

    def fail(self, article_id, _token, error):
        self.rows[article_id].update(publish_status="retryable", publish_claim_token=None)
        self.failures.append(error)
        return True

    def defer_capacity(self, article_id, _token, error):
        self.rows[article_id].update(publish_status="quota_deferred", publish_attempt_count=0,
                                     publish_claim_token=None, publish_last_error=error)
        return True

    def require_manual_reconciliation(self, article_id, _token, error):
        self.rows[article_id].update(publish_status="manual_review", publish_claim_token=None,
                                     publish_last_error=error)
        return True

    def get_external_post_state(self, article_id):
        return deepcopy(self.rows[article_id])

    def save_legacy_external_post(self, article_id, post_id, post_url):
        return self.save_external_post(article_id, None, post_id, post_url)

    def begin_legacy_creation(self, article_id, **_kwargs):
        self.rows[article_id].update(publish_attempt_count=1, publish_status="retryable")
        return True

    def defer_legacy_capacity(self, article_id, reason):
        return self.defer_capacity(article_id, None, reason)

    def defer_legacy_freshness(self, article_id):
        return self.require_manual_reconciliation(article_id, None, STALE_BACKLOG)

    def defer_legacy_manual_reconciliation(self, article_id):
        return self.require_manual_reconciliation(article_id, None, "post_identity_manual_reconciliation_required")

    def fetch_unpublished(self, *, limit, existing_posts_only=False, reserved_posts_allowed=False, **_kwargs):
        result = [deepcopy(row) for row in self.rows.values() if not row["published"]
                  and row["publish_status"] != "manual_review"]
        if existing_posts_only:
            result = [row for row in result if row["wp_post_id"] is not None or (
                reserved_posts_allowed and self.capacity and self.capacity.unattempted_today(row["id"])
            )]
        return result[:limit]


class Response:
    def __init__(self, payload, status_code=200):
        self.payload, self.status_code = payload, status_code
        self.text = "synthetic HTTP response"

    def json(self):
        return self.payload


class WordPressHTTP:
    def __init__(self):
        self.creates = []
        self.metadata = {}
        self.metadata_failure = False
        self.create_timeout = False
        self.lock = threading.RLock()

    def post(self, url, *, json, **_kwargs):
        with self.lock:
            if url.rstrip("/").endswith("/posts"):
                post_id = 501 + len(self.creates)
                self.creates.append(deepcopy(json))
                if self.create_timeout:
                    raise requests.ReadTimeout("synthetic ambiguous CREATE")
                return Response({"id": post_id, "link": f"{ORIGIN}/posts/{post_id}/"}, 201)
            post_id = int(url.rsplit("/", 1)[1])
            if self.metadata_failure:
                return Response({"code": "synthetic metadata rejection"}, 503)
            self.metadata[post_id] = dict(json["meta"])
            return self.get(url)

    def get(self, url, **_kwargs):
        post_id = int(url.rsplit("/", 1)[1])
        return Response({"id": post_id, "link": f"{ORIGIN}/posts/{post_id}/",
                         "type": "post", "status": "publish", "meta": self.metadata.get(post_id, {})})


@contextmanager
def wordpress_boundaries(http):
    with ExitStack() as stack:
        stack.enter_context(patch.object(publisher, "session", http))
        stack.enter_context(patch.object(publisher, "WP_API_URL", ORIGIN))
        stack.enter_context(patch.object(publisher, "PUBLISH_DAILY_HARD_CAP_ENABLED", False))
        stack.enter_context(patch.object(publisher, "ensure_category", return_value=1))
        stack.enter_context(patch.object(publisher, "ensure_term", return_value=2))
        stack.enter_context(patch.object(publisher, "link_markets", side_effect=lambda value: value))
        stack.enter_context(patch.object(publisher.mysql.connector, "connect",
                                        side_effect=AssertionError("No real database access permitted")))
        yield


def service_for(repository, capacity, *, enabled=True, image=None, duplicate_mode="off", duplicate_guard=None):
    adapter = publisher.WordPressPublisher()
    adapter.find_external_media = Mock(return_value=None)
    preparer = image or Mock(return_value=(None, {}))
    return PublishingService(repository, adapter, preparer, capacity_repository=capacity,
                             capacity_enabled=enabled, fresh_start_after=None,
                             duplicate_mode=duplicate_mode,
                             duplicate_guard_factory=(lambda _raw_id: duplicate_guard) if duplicate_guard else None), preparer


class RecordingDuplicateGuard:
    """Model the existing guard boundary without database or policy calls."""

    mode = "enforce"

    def __init__(self, events, *, reject_at=None):
        self.events = events
        self.reject_at = reject_at
        self.checks = 0
        self.decision = None

    @contextmanager
    def hold(self):
        self.events.append("lock")
        try:
            yield self
        finally:
            self.events.append("unlock")

    def check(self):
        self.checks += 1
        self.events.append(f"guard:{self.checks}")
        self.decision = DuplicateDecision(
            mode="enforce", stage="pre_publish", article_id=101,
            available=True, reject=self.reject_at == self.checks,
        )
        if self.decision.reject:
            raise PublicationDuplicateRejected(self.decision)


class CapacityPublishingTests(unittest.TestCase):
    def test_existing_duplicate_guard_blocks_durable_capacity_marker_at_both_checks(self):
        for reject_at in (1, 2):
            with self.subTest(reject_at=reject_at):
                events, capacity, http = [], SharedCapacity(), WordPressHTTP()
                repository = ApplicationRepository([article_row()], capacity)
                repository.reject_duplicate = Mock(return_value=True)
                guard = RecordingDuplicateGuard(events, reject_at=reject_at)
                service, image = service_for(repository, capacity, duplicate_mode="enforce", duplicate_guard=guard)
                with wordpress_boundaries(http):
                    result = service.publish_due(1)
                self.assertEqual(result["duplicates_rejected"], 1)
                self.assertEqual(result["failed"], 0)
                self.assertEqual(result["quota_deferred"], 0)
                self.assertEqual(capacity.reserve_calls, reject_at - 1)
                self.assertEqual(capacity.begin_calls, 0)
                self.assertEqual(image.call_count, reject_at - 1)
                self.assertEqual(http.creates, [])
                self.assertTrue(all(not row["started"] for row in capacity.rows.values()))
                repository.reject_duplicate.assert_called_once()

    def test_existing_duplicate_guard_and_capacity_both_allow_one_durable_create(self):
        events, capacity, http = [], SharedCapacity(29), WordPressHTTP()
        repository = ApplicationRepository([article_row()], capacity)
        guard = RecordingDuplicateGuard(events)
        image = Mock(side_effect=lambda _row: (events.append("image") or None, {}))
        service, _ = service_for(repository, capacity, image=image,
                                 duplicate_mode="enforce", duplicate_guard=guard)
        reserve, begin_create = capacity.reserve, capacity.begin_create

        def reserve_slot(*args):
            events.append("reserve")
            return reserve(*args)

        def begin(*args):
            events.append("capacity")
            return begin_create(*args)

        with wordpress_boundaries(http), patch.object(capacity, "reserve", side_effect=reserve_slot), \
             patch.object(capacity, "begin_create", side_effect=begin):
            result = service.publish_due(1)
        self.assertEqual(events, ["lock", "guard:1", "reserve", "image", "guard:2", "capacity", "reserve", "unlock"])
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["duplicates_rejected"], 0)
        self.assertEqual(result["quota_deferred"], 0)
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(capacity.rows[1]["wp_post_id"], 501)

    def test_full_capacity_skips_claim_image_and_create_in_sequential_executions(self):
        capacity, http = SharedCapacity(30), WordPressHTTP()
        repository = ApplicationRepository([article_row(is_breaking=1)], capacity)
        service, image = service_for(repository, capacity)
        with wordpress_boundaries(http):
            for _ in range(2):
                result = service.publish_due(3)
                self.assertEqual(result["attempted"], 0)
                self.assertEqual(result["failed"], 0)
                self.assertTrue(result["quota_exhausted"])
        self.assertEqual(http.creates, [])
        image.assert_not_called()
        self.assertEqual(repository.rows[1]["publish_attempt_count"], 0)
        self.assertEqual(repository.rows[1]["published"], 0)

    def test_29_existing_slots_allow_one_create_across_sequential_calls(self):
        capacity, http = SharedCapacity(29), WordPressHTTP()
        repository = ApplicationRepository([article_row(1), article_row(2, is_breaking=1)], capacity)
        service, image = service_for(repository, capacity)
        with wordpress_boundaries(http):
            first = service.publish_due(3)
            second = service.publish_due(3)
        self.assertEqual((first["succeeded"], second["succeeded"]), (1, 0))
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(image.call_count, 1)
        self.assertEqual(repository.rows[2]["published"], 1)
        self.assertEqual(capacity.rows[2]["wp_post_id"], 501)

    def test_concurrent_services_only_one_new_create_with_last_slot(self):
        capacity, http = SharedCapacity(29), WordPressHTTP()
        services = [service_for(ApplicationRepository([article_row(article_id)], capacity), capacity)[0]
                    for article_id in (1, 2)]
        barrier = threading.Barrier(2)
        snapshot = capacity.snapshot
        def simultaneous_snapshot():
            # Both callers observe the last free slot before either reserves it.
            observed = snapshot()
            barrier.wait(timeout=5)
            return observed
        with wordpress_boundaries(http), patch.object(capacity, "snapshot", side_effect=simultaneous_snapshot), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda service: service.publish_due(1), services))
        self.assertEqual(sum(result["succeeded"] for result in results), 1)
        self.assertEqual(sum(result["attempted"] for result in results), 2)
        self.assertEqual(sum(result["quota_deferred"] for result in results), 1)
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(len(capacity.rows), 1)

    def test_fresh_carryover_counts_against_current_day(self):
        capacity, http = SharedCapacity(29), WordPressHTTP()
        repository = ApplicationRepository([article_row(selected_at=NOW - timedelta(days=1))], capacity)
        service, _ = service_for(repository, capacity)
        with wordpress_boundaries(http):
            result = service.publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(capacity.rows[1]["date"], NOW.date())
        self.assertEqual(http.creates[0]["date_gmt"], NOW.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_stale_carryover_requires_review_without_reservation_or_image(self):
        capacity, http = SharedCapacity(), WordPressHTTP()
        repository = ApplicationRepository([article_row(source_publish_date=NOW - timedelta(hours=37),
                                                       selected_at=NOW - timedelta(days=1))], capacity)
        service, image = service_for(repository, capacity)
        with wordpress_boundaries(http):
            result = service.publish_due(1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["freshness_deferred"], 1)
        self.assertEqual(repository.rows[1]["publish_status"], "manual_review")
        self.assertEqual(repository.rows[1]["publish_last_error"], STALE_BACKLOG)
        self.assertFalse(repository.rows[1]["published"])
        self.assertEqual(capacity.rows, {})
        image.assert_not_called()
        self.assertEqual(http.creates, [])

    def test_seo_failure_keeps_slot_then_reconciles_existing_id_at_full_capacity(self):
        capacity, http = SharedCapacity(29), WordPressHTTP()
        repository = ApplicationRepository([article_row()], capacity)
        service, image = service_for(repository, capacity)
        http.metadata_failure = True
        with wordpress_boundaries(http):
            first = service.publish_due(1)
            self.assertEqual(first["failed"], 1)
            self.assertEqual(repository.rows[1]["wp_post_id"], 501)
            self.assertEqual(capacity.snapshot().remaining, 0)
            http.metadata_failure = False
            second = service.publish_due(1)
        self.assertEqual(second["succeeded"], 1)
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(capacity.begin_calls, 1)
        self.assertEqual(image.call_count, 1)

    def test_ambiguous_create_intent_is_retained_and_no_second_create_is_issued(self):
        capacity, http = SharedCapacity(28), WordPressHTTP()
        repository = ApplicationRepository([article_row()], capacity)
        service, image = service_for(repository, capacity)
        http.create_timeout = True
        with wordpress_boundaries(http):
            first = service.publish_due(1)
            second = service.publish_due(1)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["failed"], 1)
        self.assertTrue(capacity.rows[1]["started"])
        self.assertEqual(capacity.snapshot().remaining, 1)
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(repository.rows[1]["publish_status"], "manual_review")
        self.assertEqual(image.call_count, 1)

    def test_capacity_loss_after_claim_defers_without_failure_and_preserves_freshness(self):
        capacity, http = SharedCapacity(), WordPressHTTP()
        capacity.force_reserve_none = True
        repository = ApplicationRepository([article_row()], capacity)
        service, image = service_for(repository, capacity)
        with wordpress_boundaries(http):
            first = service.publish_due(1)
            self.assertEqual(first["quota_deferred"], 1)
            self.assertEqual(first["failed"], 0)
            self.assertEqual(repository.rows[1]["publish_status"], "quota_deferred")
            self.assertEqual(repository.rows[1]["publish_attempt_count"], 0)
            self.assertIsNone(repository.rows[1]["publish_claim_token"])
            image.assert_not_called()
            capacity.force_reserve_none = False
            second = service.publish_due(1)
        self.assertEqual(second["succeeded"], 1)
        self.assertEqual(len(http.creates), 1)

    def test_committed_capacity_marker_with_lost_acknowledgement_cannot_create_on_retry(self):
        capacity, http = SharedCapacity(28), WordPressHTTP()
        repository = ApplicationRepository([article_row()], capacity)
        service, image = service_for(repository, capacity)
        begin_create = capacity.begin_create

        def commit_then_lose_acknowledgement(article_id, key):
            begin_create(article_id, key)
            raise CapacityUnavailable('synthetic lost commit acknowledgement')

        with wordpress_boundaries(http), patch.object(
            capacity, 'begin_create', side_effect=commit_then_lose_acknowledgement,
        ):
            first = service.publish_due(1)
            self.assertEqual(first['quota_deferred'], 1)
            self.assertEqual(first['capacity_unavailable'], 1)
            self.assertEqual(first['failed'], 0)
            self.assertEqual(repository.rows[1]['publish_attempt_count'], 0)
            self.assertTrue(capacity.rows[1]['started'])
            second = service.publish_due(1)

        self.assertEqual(second['failed'], 1)
        self.assertEqual(repository.rows[1]['publish_status'], 'manual_review')
        self.assertEqual(capacity.snapshot().remaining, 1)
        self.assertEqual(http.creates, [])
        self.assertEqual(image.call_count, 1)

    def test_image_failure_at_last_reserved_slot_can_retry_without_second_slot(self):
        capacity, http = SharedCapacity(29), WordPressHTTP()
        repository = ApplicationRepository([article_row()], capacity)
        image = Mock(side_effect=[RuntimeError("synthetic image interruption"), (None, {})])
        service, _ = service_for(repository, capacity, image=image)
        with wordpress_boundaries(http):
            first = service.publish_due(1)
            second = service.publish_due(1)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(len(capacity.rows), 1)
        self.assertEqual(http.creates, [])
        # Existing retry provenance remains conservative; a pre-CREATE failure
        # must not let ordinary reconciliation permit an unverified CREATE.
        self.assertEqual(repository.rows[1]["publish_status"], "manual_review")

    def test_midnight_during_image_work_uses_capacity_authorized_date_gmt(self):
        before = NOW.replace(hour=23, minute=59, second=59)
        after = before + timedelta(seconds=2)
        capacity, http = SharedCapacity(now=before), WordPressHTTP()
        repository = ApplicationRepository([article_row(source_publish_date=before)], capacity)
        def image(_row):
            capacity.now = after
            return None, {}
        service, _ = service_for(repository, capacity, image=Mock(side_effect=image))
        with wordpress_boundaries(http):
            result = service.publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(capacity.rows[1]["date"], after.date())
        self.assertEqual(http.creates[0]["date_gmt"], after.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_missing_schema_defers_new_posts_but_still_reconciles_known_ids(self):
        capacity, http = SharedCapacity(), WordPressHTTP()
        capacity.unavailable = True
        repository = ApplicationRepository([
            article_row(1), article_row(2, wp_post_id=700, wp_post_url=f"{ORIGIN}/posts/700/",
                                      publish_status="retryable", publish_attempt_count=2),
        ], capacity)
        service, image = service_for(repository, capacity)
        with wordpress_boundaries(http):
            result = service.publish_due(3)
        self.assertEqual(result["succeeded"], 1)
        self.assertTrue(result["capacity_unavailable"])
        self.assertFalse(repository.rows[1]["published"])
        self.assertEqual(http.creates, [])
        image.assert_not_called()

    def test_disabled_capacity_preserves_new_publication_path_without_capacity_calls(self):
        capacity, http = SharedCapacity(30), WordPressHTTP()
        capacity.unavailable = True
        repository = ApplicationRepository([article_row()], capacity)
        service, image = service_for(repository, capacity, enabled=False)
        with wordpress_boundaries(http):
            result = service.publish_due(1)
        self.assertEqual(result, {"attempted": 1, "succeeded": 1, "failed": 0})
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(image.call_count, 1)
        self.assertEqual(capacity.rows, {})


class DirectWordPressCapacityTests(unittest.TestCase):
    def test_final_duplicate_guard_precedes_capacity_marker_creation_intent_and_post(self):
        events, http = [], WordPressHTTP()
        original_post = http.post

        def post(*args, **kwargs):
            if args[0].rstrip("/").endswith("/posts"):
                events.append("post")
            return original_post(*args, **kwargs)

        def authorize():
            events.append("capacity")
            return NOW

        context = PublicationContext(
            NOW, creation_is_fresh=True,
            pre_create_duplicate_guard=lambda: events.append("guard"),
            authorize_capacity_create=authorize,
            persist_creation_intent=lambda: events.append("intent"),
        )
        with wordpress_boundaries(http), patch.object(http, "post", side_effect=post):
            result = publisher.WordPressPublisher().publish(PublicationArticle.from_mapping(article_row()), None, context)
        self.assertTrue(result.success)
        self.assertEqual(events, ["guard", "capacity", "intent", "post"])
        self.assertEqual(http.creates[0]["date_gmt"], NOW.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_direct_adapter_missing_schema_fails_closed_before_post(self):
        http = WordPressHTTP()
        context = PublicationContext(NOW, publication_key="coincourier:1:101", raw_article_id=101,
                                     rich_article_id=1, creation_is_fresh=True)
        capacity = Mock()
        capacity.begin_create.side_effect = CapacityUnavailable("synthetic missing schema")
        with wordpress_boundaries(http), \
             patch.object(publisher, "PUBLISH_DAILY_HARD_CAP_ENABLED", True), \
             patch.object(publisher, "CapacityRepository", return_value=capacity):
            result = publisher.WordPressPublisher().publish(PublicationArticle.from_mapping(article_row()), None, context)
        self.assertTrue(result.deferred)
        self.assertEqual(result.error, CAPACITY_UNAVAILABLE)
        self.assertEqual(http.creates, [])

    def test_direct_callback_quota_race_blocks_post_and_uses_no_new_date(self):
        http = WordPressHTTP()
        context = PublicationContext(NOW, creation_is_fresh=True, authorize_capacity_create=lambda: None)
        with wordpress_boundaries(http):
            result = publisher.WordPressPublisher().publish(PublicationArticle.from_mapping(article_row()), None, context)
        self.assertEqual(result.error, QUOTA_EXHAUSTED)
        self.assertTrue(result.deferred)
        self.assertEqual(http.creates, [])


class CapacityDeferralStateTests(unittest.TestCase):
    """Evaluate the real SQL ownership/provenance predicates in a local fixture."""

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        self.database.executescript("""
            CREATE TABLE rich_crpytonews (
                id INTEGER PRIMARY KEY, published INTEGER DEFAULT 0,
                publish_status TEXT DEFAULT 'fresh', publish_attempt_count INTEGER DEFAULT 0,
                publish_claim_token TEXT, publish_claimed_at TEXT, publish_last_error TEXT,
                wp_post_id INTEGER, wp_post_url TEXT, wp_post_created_at TEXT
            );
            INSERT INTO rich_crpytonews (id) VALUES (11);
        """)
        self.repository = PublicationRepository(connect=lambda: SQLiteConnection(self.database))

    def update(self, **values):
        columns = ", ".join(f"{key} = ?" for key in values)
        self.database.execute(f"UPDATE rich_crpytonews SET {columns} WHERE id = 11", tuple(values.values()))
        self.database.commit()

    def state(self):
        return self.database.execute(
            """SELECT published, publish_status, publish_attempt_count,
                      publish_claim_token, wp_post_id, publish_last_error
               FROM rich_crpytonews WHERE id = 11""",
        ).fetchone()

    def test_quota_deferral_restores_first_attempt_provenance_in_actual_sql(self):
        self.update(publish_status="claimed", publish_attempt_count=1, publish_claim_token="owner")
        self.assertTrue(self.repository.defer_capacity(11, "owner", QUOTA_EXHAUSTED))
        self.assertEqual(self.state(), (0, "quota_deferred", 0, None, None, QUOTA_EXHAUSTED))

    def test_deferral_cannot_reset_prior_attempt_post_identity_or_another_owner(self):
        cases = (
            {"publish_attempt_count": 2},
            {"wp_post_id": 700},
            {"wp_post_url": f"{ORIGIN}/posts/700/"},
            {"wp_post_created_at": "2026-10-15 00:00:00"},
            {"publish_claim_token": "different-owner"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.update(publish_status="claimed", publish_attempt_count=1, publish_claim_token="owner",
                            wp_post_id=None, wp_post_url=None, wp_post_created_at=None)
                self.update(**overrides)
                before = self.state()
                self.assertFalse(self.repository.defer_capacity(11, "owner", QUOTA_EXHAUSTED))
                self.assertEqual(self.state(), before)

    def test_legacy_quota_deferral_preserves_zero_attempt_without_marking_published(self):
        self.assertTrue(self.repository.defer_legacy_capacity(11, QUOTA_EXHAUSTED))
        self.assertEqual(self.state(), (0, "quota_deferred", 0, None, None, QUOTA_EXHAUSTED))


class LegacyCapacityTests(unittest.TestCase):
    @contextmanager
    def legacy(self, repository, capacity, http, *, duplicate_guard=None):
        lock_connection = Mock()
        lock_connection.is_connected.return_value = True
        with ExitStack() as stack:
            stack.enter_context(wordpress_boundaries(http))
            stack.enter_context(patch.object(publisher, "PUBLISH_DURABLE_STATE_ENABLED", False))
            stack.enter_context(patch.object(publisher, "PUBLISH_DAILY_HARD_CAP_ENABLED", True))
            stack.enter_context(patch.object(publisher, "DUPLICATE_ENFORCEMENT_MODE",
                                            "enforce" if duplicate_guard else "off"))
            if duplicate_guard is not None:
                stack.enter_context(patch.object(publisher, "PublicationDuplicateGuard", return_value=duplicate_guard))
            stack.enter_context(patch.object(publisher.mysql.connector, "connect", return_value=lock_connection))
            stack.enter_context(patch.object(publisher, "_get_lock", return_value=True))
            stack.enter_context(patch.object(publisher, "_release_lock", return_value=True))
            stack.enter_context(patch.object(publisher, "_count_due_now", return_value=len(repository.rows)))
            stack.enter_context(patch.object(publisher, "fetch_unpublished", side_effect=repository.fetch_unpublished))
            stack.enter_context(patch.object(publisher, "PublicationRepository", return_value=repository))
            stack.enter_context(patch.object(publisher, "CapacityRepository", return_value=capacity))
            image = stack.enter_context(patch.object(publisher, "upload_image", return_value=(None, {})))
            def mark(url):
                for row in repository.rows.values():
                    if row["news_url"] == url:
                        repository.complete(row["id"], None)
            stack.enter_context(patch.object(publisher, "mark_news_as_published", side_effect=mark))
            yield image

    def test_existing_duplicate_guard_blocks_legacy_capacity_marker_at_both_checks(self):
        for reject_at in (1, 2):
            with self.subTest(reject_at=reject_at):
                events, capacity, http = [], SharedCapacity(), WordPressHTTP()
                repository = ApplicationRepository([article_row(publish_status="quota_deferred")], capacity)
                repository.reject_duplicate = Mock(return_value=True)
                guard = RecordingDuplicateGuard(events, reject_at=reject_at)
                with self.legacy(repository, capacity, http, duplicate_guard=guard) as image:
                    result = publisher.publish_news_to_wp()
                self.assertEqual(result["duplicates_rejected"], 1)
                self.assertEqual(result["failed"], 0)
                self.assertEqual(capacity.reserve_calls, reject_at - 1)
                self.assertEqual(capacity.begin_calls, 0)
                self.assertEqual(image.call_count, reject_at - 1)
                self.assertEqual(http.creates, [])
                self.assertTrue(all(not row["started"] for row in capacity.rows.values()))
                repository.reject_duplicate.assert_called_once()

    def test_full_and_missing_schema_skip_new_legacy_posts_without_images(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                capacity, http = SharedCapacity(30), WordPressHTTP()
                capacity.unavailable = unavailable
                repository = ApplicationRepository([article_row(is_breaking=1)], capacity)
                with self.legacy(repository, capacity, http) as image:
                    result = publisher.publish_news_to_wp()
                self.assertEqual(result["status"], "capacity_unavailable" if unavailable else "quota_deferred")
                self.assertEqual(result["failed"], 0)
                image.assert_not_called()
                self.assertEqual(http.creates, [])

    def test_legacy_stale_source_with_missing_seo_retains_freshness_review_precedence(self):
        capacity, http = SharedCapacity(), WordPressHTTP()
        repository = ApplicationRepository([
            article_row(source_publish_date=NOW - timedelta(hours=37), seo_meta=""),
        ], capacity)
        with self.legacy(repository, capacity, http) as image, patch.object(
            publisher.WordPressPublisher, "_validate_yoast",
            side_effect=AssertionError("Stale carryover must be deferred before SEO validation"),
        ) as validate:
            result = publisher.publish_news_to_wp()
        self.assertEqual(result["freshness_deferred"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(repository.rows[1]["publish_status"], "manual_review")
        self.assertEqual(repository.rows[1]["publish_last_error"], STALE_BACKLOG)
        self.assertEqual(capacity.reserve_calls, 0)
        self.assertEqual(capacity.begin_calls, 0)
        self.assertEqual(http.creates, [])
        validate.assert_not_called()
        image.assert_not_called()

    def test_legacy_reservation_gate_precedes_images_and_preserves_waiting_state(self):
        capacity, http = SharedCapacity(), WordPressHTTP()
        capacity.force_reserve_none = True
        repository = ApplicationRepository([article_row()], capacity)
        with self.legacy(repository, capacity, http) as image:
            result = publisher.publish_news_to_wp()
        self.assertEqual(result["quota_deferred"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(repository.rows[1]["publish_status"], "quota_deferred")
        self.assertEqual(repository.rows[1]["publish_attempt_count"], 0)
        image.assert_not_called()
        self.assertEqual(http.creates, [])

    def test_legacy_29_slots_create_once_then_reconcile_seo_at_full_capacity(self):
        capacity, http = SharedCapacity(29), WordPressHTTP()
        repository = ApplicationRepository([article_row()], capacity)
        http.metadata_failure = True
        with self.legacy(repository, capacity, http) as image:
            first = publisher.publish_news_to_wp()
            http.metadata_failure = False
            second = publisher.publish_news_to_wp()
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["succeeded"], 1)
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(capacity.begin_calls, 1)
        self.assertEqual(image.call_count, 1)


if __name__ == "__main__":
    unittest.main()
