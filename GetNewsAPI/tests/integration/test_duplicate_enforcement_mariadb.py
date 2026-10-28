"""Opt-in same-event enforcement on one explicitly disposable local schema."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import mysql.connector

import config
from duplicate_detection.enforcement import assess_duplicate_enforcement
from duplicate_detection.publication import PublicationDuplicateGuard
from publishing.models import PublicationResult
from publishing.service import PublishingService
from repositories.duplicate_assessments import DuplicateAssessmentRepository
from repositories.publication import PublicationRepository
from repositories.raw_news import RawNewsRepository


RUN_INTEGRATION = os.getenv("RUN_MARIADB_INTEGRATION", "false").strip().lower() in {"1", "true", "yes", "on"}
HOST = os.getenv("DUPLICATE_MARIADB_TEST_HOST", "127.0.0.1")
PORT = int(os.getenv("DUPLICATE_MARIADB_TEST_PORT", "65035"))
DATABASE = os.getenv("DUPLICATE_MARIADB_TEST_DATABASE", "coincourier_duplicate_test")
USER = os.getenv("DUPLICATE_MARIADB_TEST_USER", "duplicate_test")
PASSWORD = os.getenv("DUPLICATE_MARIADB_TEST_PASSWORD", "duplicate_test_only")
REPOSITORY_DIR = Path(__file__).resolve().parents[3]


def _assert_disposable():
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("duplicate integration tests require a loopback host")
    if DATABASE != "coincourier_duplicate_test" or not DATABASE.endswith("_test"):
        raise RuntimeError("duplicate integration tests require the dedicated coincourier_duplicate_test schema")


def _connect(*, autocommit=False):
    _assert_disposable()
    return mysql.connector.connect(host=HOST, port=PORT, database=DATABASE, user=USER,
                                   password=PASSWORD, connection_timeout=5,
                                   ssl_disabled=True, autocommit=autocommit)


def _execute(sql, params=()):
    connection = _connect(autocommit=True)
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
    finally:
        cursor.close()
        connection.close()


def _rows(sql, params=()):
    connection = _connect(autocommit=True)
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()
        connection.close()


class FakePublisher:
    """Only CMS side effects are synthetic; all claims, checks and writes use SQL."""
    def __init__(self):
        self.posts, self.media = [], []

    def reconcile(self, _article, _context):
        return None

    def find_external_media(self, _key):
        return None

    def external_media_exists(self, _media_id):
        return True

    def persist_external_media_identity(self, _media_id, _key):
        return None

    def prepare_image(self, row):
        media_id = 600 + row["id"]
        self.media.append(media_id)
        return media_id, {"provider": "pexels", "asset_key": f"pexels:{media_id}"}

    def publish(self, _article, image, context):
        if context.pre_create_duplicate_guard is None:
            raise AssertionError("native publication must retain the final pre-create callback")
        context.pre_create_duplicate_guard()
        post_id = 900 + len(self.posts)
        post_url = f"https://coincourier.example/synthetic/{post_id}"
        self.posts.append(post_id)
        context.persist_external_state(post_id, post_url)
        return PublicationResult(True, external_id=post_id, external_url=post_url,
                                 media_external_id=image.external_id, created=True)


@unittest.skipUnless(RUN_INTEGRATION, "set RUN_MARIADB_INTEGRATION=true for disposable duplicate MariaDB tests")
class DuplicateEnforcementMariaDBTests(unittest.TestCase):
    def setUp(self):
        _assert_disposable()
        paths = (
            "maintenance/testing/mariadb_phase2_baseline.sql",
            "maintenance/migrations/002_phase2_durable_state.sql",
            "maintenance/migrations/004_phase2_indexes.sql",
            "maintenance/migrations/007_phase5_duplicate_shadow.sql",
        )
        connection = _connect(autocommit=True)
        cursor = connection.cursor()
        try:
            for relative in paths:
                cursor.execute((REPOSITORY_DIR / relative).read_text(encoding="utf-8"), map_results=True)
                while cursor.nextset():
                    pass
        finally:
            cursor.close()
            connection.close()

    def seed_source(self, raw_id, *, processed):
        body = (
            "Solana validators activate Alpenglow mainnet on October 28, 2026 with 1000 transactions."
            if raw_id == 10 else
            "Alpenglow mainnet activation on Solana occurs on October 28, 2026 with 1000 transactions. Validators confirm protocol details."
        )
        _execute("""
            INSERT INTO cryptonewsapi
                (id,news_id,event_id,news_url,title,full_text,publish_date,source_name,
                 processed,processing_status,chosen_for_publish,scheduled_for,selected_at)
            VALUES (%s,%s,'alpenglow-activation-20261028',%s,%s,%s,UTC_TIMESTAMP(),
                    'Synthetic independent outlet',%s,%s,1,TIMESTAMPADD(MINUTE,-1,UTC_TIMESTAMP()),UTC_TIMESTAMP())
        """, (raw_id, f"source-{raw_id}", f"https://source.example/{raw_id}",
               "Solana Alpenglow mainnet activation", body, int(processed), "completed" if processed else "pending"))

    def seed_rich(self, rich_id, raw_id, *, published=False):
        _execute("""
            INSERT INTO rich_crpytonews
                (id,raw_article_id,news_url,title,full_text,category,publish_date,
                 published,publish_status,publish_attempt_count,wp_post_id,wp_post_url,published_at)
            VALUES (%s,%s,%s,'Solana Alpenglow mainnet activation','<p>Synthetic rewritten body.</p>',
                    'General',UTC_TIMESTAMP(),%s,%s,0,%s,%s,%s)
        """, (rich_id, raw_id, f"https://source.example/{raw_id}", int(published),
               "published" if published else "fresh", 901 if published else None,
               "https://coincourier.example/canonical/901" if published else None,
               "2026-10-28 12:00:00" if published else None))

    def test_real_named_lock_allows_one_post_and_terminal_duplicate_for_concurrent_workers(self):
        for raw_id in (10, 20):
            self.seed_source(raw_id, processed=True)
            self.seed_rich(raw_id + 100, raw_id)
        cms = FakePublisher()
        start = threading.Barrier(2)
        def run():
            repository = PublicationRepository(connect=_connect)
            def guard(raw_id):
                return PublicationDuplicateGuard(raw_id, mode="enforce",
                    repository=DuplicateAssessmentRepository(connect=_connect), connection_factory=_connect)
            service = PublishingService(repository, cms, cms.prepare_image,
                fresh_start_after=None, duplicate_mode="enforce", duplicate_guard_factory=guard)
            start.wait(timeout=5)
            return service.publish_due(1)
        with patch.dict(config.DB_CONFIG, {"database": DATABASE}), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(len(cms.posts), 1)
        self.assertEqual(len(cms.media), 1)
        self.assertEqual(sum(result["succeeded"] for result in results), 1)
        self.assertEqual(sum(result["duplicates_rejected"] for result in results), 1)
        self.assertEqual(sum(result["failed"] for result in results), 0)
        rows = _rows("SELECT id,publish_status,wp_post_id FROM rich_crpytonews ORDER BY id")
        self.assertEqual(sorted(row["publish_status"] for row in rows), ["duplicate", "published"])
        duplicate = next(row for row in rows if row["publish_status"] == "duplicate")
        canonical = next(row for row in rows if row["publish_status"] == "published")
        self.assertEqual(canonical["wp_post_id"], cms.posts[0])
        self.assertIsNone(duplicate["wp_post_id"])
        raw_id = duplicate["id"] - 100
        self.assertEqual(_rows("SELECT chosen_for_publish FROM cryptonewsapi WHERE id=%s", (raw_id,))[0]["chosen_for_publish"], 0)
        audit = json.loads(_rows("SELECT reason_json FROM duplicate_assessments WHERE article_id=%s", (raw_id,))[0]["reason_json"])["enforcement"]
        self.assertEqual(audit["stage"], "pre_publish")
        self.assertEqual(audit["decision"], "reject_duplicate")
        self.assertEqual(audit["canonical_wp_id"], cms.posts[0])

    def test_raw_claim_duplicate_audit_and_terminal_disposition_prevent_reacquisition(self):
        self.seed_source(10, processed=True)
        self.seed_rich(110, 10, published=True)
        self.seed_source(20, processed=False)
        raw_repository = RawNewsRepository(connect=_connect)
        arguments = {"timeout_minutes": 30, "lookahead_minutes": None, "fresh_start_after": None}
        claim = raw_repository.claim_next(**arguments)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.article["id"], 20)
        decision = assess_duplicate_enforcement(claim.article, stage="raw", mode="enforce",
            repository=DuplicateAssessmentRepository(connect=_connect))
        self.assertTrue(decision.reject)
        self.assertTrue(decision.available)
        self.assertEqual(decision.canonical_wp_id, 901)
        self.assertTrue(raw_repository.reject_duplicate(20, token=claim.token, decision=decision))
        self.assertIsNone(raw_repository.claim_next(**arguments))
        state = _rows("SELECT processed,chosen_for_publish,scheduled_for,processing_status,processing_claim_token FROM cryptonewsapi WHERE id=20")[0]
        self.assertEqual((state["processed"], state["chosen_for_publish"], state["processing_status"]), (1, 0, "duplicate"))
        self.assertIsNone(state["scheduled_for"])
        self.assertIsNone(state["processing_claim_token"])
        audit = json.loads(_rows("SELECT reason_json FROM duplicate_assessments WHERE article_id=20 AND candidate_article_id=10")[0]["reason_json"])["enforcement"]
        self.assertEqual(audit["stage"], "raw")
        self.assertEqual(audit["decision"], "reject_duplicate")
        self.assertEqual(audit["canonical_raw_id"], 10)
        self.assertEqual(audit["canonical_wp_id"], 901)
        self.assertEqual(audit["event_id"], "alpenglow-activation-20261028")
        self.assertEqual(len(_rows("SELECT id FROM rich_crpytonews")), 1)


if __name__ == "__main__":
    unittest.main()
