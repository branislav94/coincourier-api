"""Offline quota repository contracts; native locking is covered separately."""

from __future__ import annotations

import copy
import sys
import threading
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from repositories.publication_capacity import (
    CapacityAlreadyAttempted,
    CapacityRepository,
    CapacityUnavailable,
)


NOW = datetime(2026, 10, 15, 12, 0, 0)
DAY = NOW.date()


class _Database:
    """Small transactional SQL fake with explicit, inspectable persisted state."""

    def __init__(self):
        self.now = NOW
        self.clock = deque()
        self.days = {}
        self.reservations = {}
        self.events = []
        self.engines = {
            "publication_capacity_days": "InnoDB",
            "publication_capacity_reservations": "InnoDB",
        }
        self.indexes = [
            ("publication_capacity_days", "PRIMARY", 0, "utc_date", None),
            ("publication_capacity_reservations", "PRIMARY", 0, "rich_article_id", None),
            ("publication_capacity_reservations", "uq_publication_capacity_key", 0, "publication_key", None),
            ("publication_capacity_reservations", "idx_publication_capacity_date", 1, "utc_date", None),
        ]
        self.deadlocks = 0
        self.uncertain_commit = False
        self.lock = threading.RLock()

    def connect(self):
        return _Connection(self)

    def add(self, article_id, *, day=DAY, attempted=False, post_id=None):
        self.reservations[article_id] = {
            "rich_article_id": article_id,
            "publication_key": f"article:{article_id}",
            "utc_date": day,
            "publication_at": NOW if attempted else None,
            "create_started_at": NOW if attempted else None,
            "wp_post_id": post_id,
        }


class _Connection:
    def __init__(self, db):
        self.db = db
        self.before = None

    def start_transaction(self):
        self.db.lock.acquire()
        self.before = copy.deepcopy((self.db.days, self.db.reservations))
        self.db.events.append("begin")

    def cursor(self, dictionary=False):
        return _Cursor(self.db)

    def commit(self):
        self.db.events.append("commit")
        if self.before is not None:
            self.before = None
            self.db.lock.release()
        if self.db.uncertain_commit:
            self.db.uncertain_commit = False
            raise mysql.connector.Error("synthetic lost commit acknowledgement", errno=2013)

    def rollback(self):
        self.db.events.append("rollback")
        if self.before is not None:
            self.db.days, self.db.reservations = self.before
            self.before = None
            self.db.lock.release()

    def close(self):
        self.rollback() if self.before is not None else None
        self.db.events.append("close")


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.result = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().replace("`", "").split())
        self.db.events.append((normalized, params))
        self.result = []
        self.rowcount = 0
        if "from information_schema.tables" in normalized:
            self.result = [{"table_name": table, "engine": engine} for table, engine in self.db.engines.items()]
        elif "from information_schema.statistics" in normalized:
            self.result = [
                dict(table_name=table, index_name=name, non_unique=non_unique, position=1, column_name=column, sub_part=prefix)
                for table, name, non_unique, column, prefix in self.db.indexes
            ]
        elif normalized == "select utc_timestamp() as now_utc":
            self.result = [{"now_utc": self.db.clock.popleft() if self.db.clock else self.db.now}]
        elif normalized.startswith("select utc_date() as utc_date"):
            day = self.db.now.date()
            guard = self.db.days.get(day, {})
            self.result = [{
                "utc_date": day,
                "guard_date": guard.get("utc_date"),
                "hard_cap": guard.get("hard_cap"),
                "baseline_count": guard.get("baseline_count"),
                "reserved_count": sum(row["utc_date"] == day for row in self.db.reservations.values()),
            }]
        elif normalized.startswith("insert into publication_capacity_days"):
            if self.db.deadlocks:
                self.db.deadlocks -= 1
                raise mysql.connector.Error("synthetic deadlock", errno=1213)
            day, cap = params
            self.db.days.setdefault(day, {"utc_date": day, "hard_cap": cap, "baseline_count": 0})
        elif normalized.startswith("select utc_date, hard_cap, baseline_count"):
            row = self.db.days.get(params[0])
            self.result = [dict(row)] if row is not None else []
        elif normalized.startswith("select rich_article_id, publication_key"):
            article_id, key = params
            self.result = [dict(row) for row in self.db.reservations.values() if row["rich_article_id"] == article_id or row["publication_key"] == key]
        elif normalized.startswith("select rich_article_id from publication_capacity_reservations"):
            self.result = [{"rich_article_id": row["rich_article_id"]} for row in self.db.reservations.values() if row["utc_date"] == params[0]]
        elif normalized.startswith("insert into publication_capacity_reservations"):
            article_id, key, day = params
            if article_id in self.db.reservations or any(row["publication_key"] == key for row in self.db.reservations.values()):
                raise mysql.connector.Error("synthetic unique key violation", errno=1062)
            self.db.add(article_id, day=day)
            self.db.reservations[article_id]["publication_key"] = key
            self.rowcount = 1
        elif normalized.startswith("update publication_capacity_reservations set utc_date"):
            day, article_id, key = params
            row = self.db.reservations[article_id]
            if row["publication_key"] == key:
                row["utc_date"] = day
                self.rowcount = 1
        elif normalized.startswith("update publication_capacity_reservations set publication_at"):
            publication_at, started_at, article_id, key = params
            row = self.db.reservations[article_id]
            if row["publication_key"] == key and row["create_started_at"] is None:
                row.update(publication_at=publication_at, create_started_at=started_at)
                self.rowcount = 1
        elif normalized.startswith("update publication_capacity_reservations set wp_post_id"):
            post_id, article_id, key = params
            row = self.db.reservations[article_id]
            if row["publication_key"] == key:
                row["wp_post_id"] = post_id
                self.rowcount = 1
        else:
            raise AssertionError(f"unexpected capacity SQL: {normalized}")

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result

    def close(self):
        pass


class PublicationCapacityTests(unittest.TestCase):
    def setUp(self):
        self.db = _Database()
        self.repository = CapacityRepository(30, connect=self.db.connect)

    def test_twenty_nine_reservations_allow_only_one_further_create(self):
        for article_id in range(1, 30):
            self.db.add(article_id, attempted=True)
        stamp = self.repository.begin_create(30, "article:30")
        self.assertEqual(stamp, NOW.replace(tzinfo=timezone.utc))
        self.assertIsNone(self.repository.begin_create(31, "article:31"))
        self.assertEqual(len(self.db.reservations), 30)

    def test_full_day_keeps_waiting_identity_unreserved(self):
        for article_id in range(1, 31):
            self.db.add(article_id)
        self.assertIsNone(self.repository.reserve(31, "article:31"))
        self.assertIsNone(self.repository.begin_create(31, "article:31"))
        self.assertEqual(self.repository.snapshot().remaining, 0)
        self.assertNotIn(31, self.db.reservations)

    def test_same_identity_reuses_one_slot_until_create_is_started(self):
        first = self.repository.reserve(1, "article:1")
        second = CapacityRepository(30, connect=self.db.connect).reserve(1, "article:1")
        self.assertEqual(first, second)
        self.assertEqual(self.repository.snapshot().remaining, 29)
        stamp = self.repository.begin_create(1, "article:1")
        self.assertEqual(stamp.date(), DAY)
        self.assertEqual(len(self.db.reservations), 1)
        with self.assertRaises(CapacityAlreadyAttempted):
            self.repository.begin_create(1, "article:1")
        with self.assertRaises(CapacityAlreadyAttempted):
            self.repository.reserve(1, "article:1")

    def test_full_day_still_allows_the_identity_that_owns_the_final_slot(self):
        for article_id in range(1, 31):
            self.db.add(article_id)
        self.assertIsNotNone(self.repository.reserve(30, "article:30"))
        self.assertIsNotNone(self.repository.begin_create(30, "article:30"))
        self.assertEqual(len(self.db.reservations), 30)

    def test_connection_failure_is_not_reported_as_available_capacity(self):
        def unavailable():
            raise mysql.connector.Error("synthetic unavailable database", errno=2003)

        repository = CapacityRepository(30, connect=unavailable)
        with self.assertRaises(CapacityUnavailable):
            repository.snapshot()
        with self.assertRaises(CapacityUnavailable):
            repository.begin_create(1, "article:1")

    def test_partial_persisted_create_identity_never_authorizes_a_fresh_create(self):
        for field, value in (("publication_at", NOW), ("wp_post_id", 1001)):
            with self.subTest(field=field):
                self.setUp()
                self.db.add(1)
                self.db.reservations[1][field] = value
                with self.assertRaises(CapacityAlreadyAttempted):
                    self.repository.begin_create(1, "article:1")

    def test_ambiguous_commit_remains_a_create_fence(self):
        self.db.uncertain_commit = True
        with self.assertRaises(CapacityUnavailable):
            self.repository.begin_create(1, "article:1")
        self.assertIsNotNone(self.db.reservations[1]["create_started_at"])
        with self.assertRaises(CapacityAlreadyAttempted):
            self.repository.begin_create(1, "article:1")

    def test_confirmed_post_and_seo_retry_preserve_original_day_and_slot(self):
        self.repository.begin_create(1, "article:1")
        self.repository.record_created(1, "article:1", 1001)
        self.db.now += timedelta(days=1)
        self.repository.record_created(1, "article:1", 1001)
        self.assertEqual(self.db.reservations[1]["utc_date"], DAY)
        self.assertEqual(self.db.reservations[1]["wp_post_id"], 1001)
        self.assertEqual(self.repository.snapshot().remaining, 30)
        with self.assertRaises(CapacityAlreadyAttempted):
            self.repository.begin_create(1, "article:1")

    def test_post_identity_conflict_does_not_overwrite_saved_post(self):
        self.repository.begin_create(1, "article:1")
        self.repository.record_created(1, "article:1", 1001)
        with self.assertRaises(CapacityUnavailable):
            self.repository.record_created(1, "article:1", 1002)
        self.assertEqual(self.db.reservations[1]["wp_post_id"], 1001)

    def test_midnight_moves_only_unattempted_reservation_to_new_day(self):
        old_day = DAY - timedelta(days=1)
        self.db.add(1, day=old_day)
        stamp = self.repository.begin_create(1, "article:1")
        self.assertEqual(stamp.date(), DAY)
        self.assertEqual(self.db.reservations[1]["utc_date"], DAY)
        self.assertEqual(self.db.reservations[1]["publication_at"], stamp.replace(tzinfo=None))

    def test_midnight_full_day_defers_and_preserves_old_reservation(self):
        old_day = DAY - timedelta(days=1)
        self.db.add(31, day=old_day)
        for article_id in range(1, 31):
            self.db.add(article_id)
        self.assertIsNone(self.repository.begin_create(31, "article:31"))
        self.assertEqual(self.db.reservations[31]["utc_date"], old_day)
        self.assertIsNone(self.db.reservations[31]["create_started_at"])

    def test_waiting_for_guard_across_midnight_restarts_for_new_day(self):
        before = datetime(2026, 10, 15, 23, 59, 59)
        midnight = datetime(2026, 10, 16, 0, 0, 0)
        self.db.now = midnight
        self.db.clock.extend([before, midnight])
        stamp = self.repository.begin_create(1, "article:1")
        self.assertEqual(stamp, midnight.replace(tzinfo=timezone.utc))
        self.assertEqual(set(self.db.days), {midnight.date()})
        self.assertIn("rollback", self.db.events)

    def test_waiting_for_identity_across_midnight_restarts_for_new_day(self):
        before = datetime(2026, 10, 15, 23, 59, 59)
        midnight = datetime(2026, 10, 16, 0, 0, 0)
        self.db.now = midnight
        self.db.clock.extend([before, before, midnight])
        stamp = self.repository.begin_create(1, "article:1")
        self.assertEqual(stamp, midnight.replace(tzinfo=timezone.utc))
        self.assertEqual(self.db.reservations[1]["utc_date"], midnight.date())

    def test_verified_midday_baseline_consumes_capacity(self):
        self.db.days[DAY] = {"utc_date": DAY, "hard_cap": 30, "baseline_count": 29}
        self.assertEqual(self.repository.snapshot().remaining, 1)
        self.assertIsNotNone(self.repository.begin_create(1, "article:1"))
        self.assertIsNone(self.repository.begin_create(2, "article:2"))

    def test_conflicting_guard_and_overcommit_fail_closed(self):
        for baseline, cap in ((0, 31), (31, 30)):
            with self.subTest(baseline=baseline, cap=cap):
                self.db.days[DAY] = {"utc_date": DAY, "hard_cap": cap, "baseline_count": baseline}
                with self.assertRaises(CapacityUnavailable):
                    self.repository.snapshot()
                with self.assertRaises(CapacityUnavailable):
                    self.repository.begin_create(1, "article:1")
                self.assertEqual(self.db.reservations, {})

    def test_conflicting_stable_article_or_publication_identity_fails_closed(self):
        self.repository.reserve(1, "article:1")
        for article_id, key in ((1, "different"), (2, "article:1")):
            with self.subTest(article_id=article_id, key=key):
                with self.assertRaises(CapacityUnavailable):
                    self.repository.reserve(article_id, key)
        self.assertEqual(len(self.db.reservations), 1)

    def test_missing_nontransactional_or_unconstrained_schema_fails_closed(self):
        scenarios = [
            lambda: self.db.engines.pop("publication_capacity_days"),
            lambda: self.db.engines.update(publication_capacity_reservations="MyISAM"),
            lambda: self.db.indexes.pop(2),
            lambda: self.db.indexes.pop(0),
            lambda: self.db.indexes.__setitem__(2, ("publication_capacity_reservations", "uq_publication_capacity_key", 0, "publication_key", 100)),
        ]
        for scenario in scenarios:
            self.setUp()
            scenario()
            with self.assertRaises(CapacityUnavailable):
                self.repository.reserve(1, "article:1")
            self.assertEqual(self.db.reservations, {})

    def test_read_only_snapshot_does_not_create_guard_or_reservation(self):
        snapshot = self.repository.snapshot()
        self.assertEqual((snapshot.utc_date, snapshot.remaining, snapshot.limit), (DAY, 30, 30))
        self.assertEqual(self.db.days, {})
        self.assertEqual(self.db.reservations, {})
        self.assertNotIn("begin", self.db.events)
        self.assertFalse(any(isinstance(event, tuple) and event[0].startswith(("insert", "update")) for event in self.db.events))

    def test_deadlocks_retry_only_whole_bounded_transactions(self):
        self.db.deadlocks = 1
        self.assertIsNotNone(self.repository.reserve(1, "article:1"))
        self.assertEqual(self.db.events.count("begin"), 2)
        self.assertEqual(len(self.db.reservations), 1)
        self.db.deadlocks = 3
        with self.assertRaises(CapacityUnavailable):
            self.repository.reserve(2, "article:2")
        self.assertEqual(self.db.events.count("begin"), 5)
        self.assertNotIn(2, self.db.reservations)

    def test_invalid_configuration_and_identity_are_rejected(self):
        for cap in (0, -1, True, 1.5, 4294967296):
            with self.assertRaises(ValueError):
                CapacityRepository(cap, connect=self.db.connect)
        for article_id, key in ((0, "valid"), (True, "valid"), (1, ""), (1, "a" * 192)):
            with self.assertRaises(ValueError):
                self.repository.reserve(article_id, key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
