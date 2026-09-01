from __future__ import annotations

import inspect
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import mysql.connector


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
MIGRATION_DIR = REPOSITORY_DIR / "maintenance" / "migrations"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config
import gpt_processor
import publish_to_wp
from publishing.models import PublicationArticle, PublicationContext, PublicationResult
from publishing.service import PublishingService, build_publication_key
from publishing.wordpress import persistence, publisher
from repositories.publication import PublicationClaim, PublicationRepository
from repositories.raw_news import ProcessingClaim, RawNewsRepository
from repositories.state import claim_prefix, is_claim_deadlock, safe_error_message


ARTICLE = {
    "id": 8,
    "raw_article_id": 3,
    "durable_raw_article_id": 3,
    "news_url": "https://news.example/one",
    "title": "Durable Article",
    "full_text": "Markets remain active.",
    "category": "Markets",
    "hashtags": "Bitcoin",
    "seo_slug": "durable-article",
    "seo_focus": "Durable focus",
    "seo_meta": "Durable description",
    "seo_canonical": "https://canonical.example/one",
    "wp_post_id": None,
    "wp_media_id": None,
    "wp_media_metadata_json": None,
}


class FakeCursor:
    def __init__(self, database, dictionary=False):
        self.database = database
        self.dictionary = dictionary
        self.rowcount = 0
        self.result = None

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        self.database.events.append(("execute", normalized, params))
        self.rowcount = 0
        self.result = None

        if normalized.startswith("select * from cryptonewsapi"):
            row = self.database.raw
            state = row.get("processing_status")
            eligible = not row.get("processed") and (
                state in (None, "pending", "retryable")
                or (state == "claimed" and row.get("processing_claimed_at") == "expired")
            )
            self.result = dict(row) if eligible else None
        elif "update cryptonewsapi" in normalized and "set processed = 1" in normalized:
            row_id, token = params
            row = self.database.raw
            if self.database.raw_owner(row_id, token):
                row.update(
                    processed=1,
                    processing_status="completed",
                    processing_claim_token=None,
                    processing_claimed_at=None,
                    processing_last_error=None,
                )
                self.rowcount = 1
        elif "update cryptonewsapi" in normalized and "processing_status = 'retryable'" in normalized:
            error, row_id, token = params
            row = self.database.raw
            if self.database.raw_owner(row_id, token):
                row.update(
                    processing_status="retryable",
                    processing_claim_token=None,
                    processing_claimed_at=None,
                    processing_last_error=error,
                )
                self.rowcount = 1
        elif "update cryptonewsapi" in normalized and "set processing_status = 'claimed'" in normalized:
            token, row_id, _timeout = params
            row = self.database.raw
            if row["id"] == row_id and not row["processed"]:
                row.update(
                    processing_status="claimed",
                    processing_claim_token=token,
                    processing_claimed_at="active",
                    processing_attempt_count=row.get("processing_attempt_count", 0) + 1,
                    processing_last_error=None,
                )
                self.rowcount = 1
        elif normalized.startswith("select r.*, c.id as durable_raw_article_id"):
            row = self.database.rich
            state = row.get("publish_status")
            eligible = not row.get("published") and (
                state in (None, "pending", "retryable")
                or (
                    state in ("claimed", "post_created")
                    and row.get("publish_claimed_at") == "expired"
                )
            )
            if eligible:
                self.result = {**row, "durable_raw_article_id": 3}
        elif "update rich_crpytonews" in normalized and "publish_status = 'claimed'" in normalized:
            token, row_id, _timeout = params
            row = self.database.rich
            if row["id"] == row_id and not row["published"]:
                row.update(
                    publish_status="claimed",
                    publish_claim_token=token,
                    publish_claimed_at="active",
                    publish_attempt_count=row.get("publish_attempt_count", 0) + 1,
                    publish_last_error=None,
                )
                self.rowcount = 1
        elif "update rich_crpytonews" in normalized and "set published = 1" in normalized:
            row_id, token = params
            row = self.database.rich
            if self.database.publish_owner(row_id, token):
                row.update(
                    published=1,
                    publish_status="published",
                    publish_claim_token=None,
                    publish_claimed_at=None,
                    publish_last_error=None,
                )
                self.rowcount = 1
        elif "update rich_crpytonews" in normalized and "publish_status = 'retryable'" in normalized:
            error, row_id, token = params
            row = self.database.rich
            if self.database.publish_owner(row_id, token):
                row.update(
                    publish_status="retryable",
                    publish_claim_token=None,
                    publish_claimed_at=None,
                    publish_last_error=error,
                )
                self.rowcount = 1

    def fetchone(self):
        return self.result

    def close(self):
        self.database.events.append("cursor-close")


class FakeConnection:
    def __init__(self, database):
        self.database = database

    def start_transaction(self):
        self.database.events.append("transaction-start")

    def cursor(self, dictionary=False):
        return FakeCursor(self.database, dictionary=dictionary)

    def commit(self):
        self.database.events.append("commit")

    def rollback(self):
        self.database.events.append("rollback")

    def close(self):
        self.database.events.append("connection-close")


class FakeClaimDatabase:
    def __init__(self):
        self.events = []
        self.raw = {
            "id": 1,
            "news_url": "https://news.example/raw",
            "processed": 0,
            "processing_status": "pending",
            "processing_claim_token": None,
            "processing_claimed_at": None,
            "processing_attempt_count": 0,
        }
        self.rich = {
            **ARTICLE,
            "published": 0,
            "publish_status": "pending",
            "publish_claim_token": None,
            "publish_claimed_at": None,
            "publish_attempt_count": 0,
        }

    def connect(self):
        return FakeConnection(self)

    def raw_owner(self, row_id, token):
        return (
            self.raw["id"] == row_id
            and self.raw.get("processing_status") == "claimed"
            and self.raw.get("processing_claim_token") == token
        )

    def publish_owner(self, row_id, token):
        return (
            self.rich["id"] == row_id
            and self.rich.get("publish_status") in ("claimed", "post_created")
            and self.rich.get("publish_claim_token") == token
        )


class ScriptedClaimCursor:
    def __init__(self, connection, dictionary=False):
        self.connection = connection
        self.delegate = FakeCursor(connection.database, dictionary=dictionary)

    @property
    def rowcount(self):
        return self.delegate.rowcount

    def execute(self, sql, params=()):
        if self.connection.error is not None:
            error = self.connection.error
            self.connection.error = None
            raise error
        self.delegate.execute(sql, params)

    def fetchone(self):
        return self.delegate.fetchone()

    def close(self):
        self.connection.cursor_closed = True
        self.delegate.close()


class ScriptedClaimConnection:
    def __init__(self, database, error):
        self.database = database
        self.error = error
        self.transaction_starts = 0
        self.commits = 0
        self.rollbacks = 0
        self.cursor_closed = False
        self.closed = False

    def start_transaction(self):
        self.transaction_starts += 1

    def cursor(self, dictionary=False):
        return ScriptedClaimCursor(self, dictionary=dictionary)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class ScriptedClaimDatabase(FakeClaimDatabase):
    def __init__(self, errors):
        super().__init__()
        self.errors = list(errors)
        self.connections = []

    def connect(self):
        error = self.errors.pop(0) if self.errors else None
        connection = ScriptedClaimConnection(self, error)
        self.connections.append(connection)
        return connection


class ProcessingRepositoryTests(unittest.TestCase):
    def test_two_processors_cannot_claim_same_raw_row(self):
        database = FakeClaimDatabase()
        repository = RawNewsRepository(connect=database.connect)
        first = repository.claim_next(timeout_minutes=30, lookahead_minutes=None, fresh_start_after=None)
        second = repository.claim_next(timeout_minutes=30, lookahead_minutes=None, fresh_start_after=None)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_expired_processor_claim_can_be_reclaimed(self):
        database = FakeClaimDatabase()
        database.raw.update(
            processing_status="claimed",
            processing_claim_token="old",
            processing_claimed_at="expired",
        )
        claim = RawNewsRepository(connect=database.connect).claim_next(
            timeout_minutes=30,
            lookahead_minutes=None,
            fresh_start_after=None,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(claim.recovered)
        self.assertNotEqual(claim.token, "old")

    def test_nonexpired_processor_claim_cannot_be_stolen(self):
        database = FakeClaimDatabase()
        database.raw.update(
            processing_status="claimed",
            processing_claim_token="owner",
            processing_claimed_at="active",
        )
        claim = RawNewsRepository(connect=database.connect).claim_next(
            timeout_minutes=30,
            lookahead_minutes=None,
            fresh_start_after=None,
        )
        self.assertIsNone(claim)

    def test_only_owner_token_completes_processing(self):
        database = FakeClaimDatabase()
        repository = RawNewsRepository(connect=database.connect)
        claim = repository.claim_next(timeout_minutes=30, lookahead_minutes=None, fresh_start_after=None)
        assert claim is not None
        self.assertFalse(repository.complete(1, "wrong"))
        self.assertTrue(repository.complete(1, claim.token))
        self.assertEqual(database.raw["processed"], 1)
        self.assertEqual(database.raw["processing_status"], "completed")

    def test_processing_failure_is_safe_and_retryable(self):
        database = FakeClaimDatabase()
        repository = RawNewsRepository(connect=database.connect)
        claim = repository.claim_next(timeout_minutes=30, lookahead_minutes=None, fresh_start_after=None)
        assert claim is not None
        message = safe_error_message(RuntimeError("Bearer secret full article"), "processing")
        self.assertTrue(repository.fail(1, claim.token, message))
        self.assertEqual(database.raw["processing_status"], "retryable")
        self.assertIsNone(database.raw["processing_claim_token"])
        self.assertNotIn("secret", database.raw["processing_last_error"])

    def test_claim_transaction_commits_before_processing_work(self):
        database = FakeClaimDatabase()
        repository = RawNewsRepository(connect=database.connect)
        claim = repository.claim_next(timeout_minutes=30, lookahead_minutes=None, fresh_start_after=None)
        self.assertIsNotNone(claim)
        self.assertLess(database.events.index("commit"), database.events.index("connection-close"))
        self.assertIn("FOR UPDATE", inspect.getsource(RawNewsRepository._claim_next_once))


class ClaimContentionPolicyTests(unittest.TestCase):
    @staticmethod
    def repository(name, database):
        if name == "raw":
            return RawNewsRepository(connect=database.connect), {
                "timeout_minutes": 30,
                "lookahead_minutes": None,
                "fresh_start_after": None,
            }
        return PublicationRepository(connect=database.connect), {
            "timeout_minutes": 30,
            "fresh_start_after": None,
        }

    @staticmethod
    def deadlock():
        return mysql.connector.errors.InternalError(
            msg="deadlock", errno=1213, sqlstate="40001"
        )

    def test_deadlock_recognition_is_errno_1213_only(self):
        deadlock = mysql.connector.errors.InternalError(
            msg="deadlock", errno=1213, sqlstate="40001"
        )
        lock_timeout = mysql.connector.errors.DatabaseError(
            msg="lock timeout", errno=1205, sqlstate="HY000"
        )
        self.assertTrue(is_claim_deadlock(deadlock))
        self.assertFalse(is_claim_deadlock(lock_timeout))

    def test_first_deadlock_retries_with_fresh_connection_and_claims(self):
        for name in ("raw", "publication"):
            with self.subTest(repository=name):
                database = ScriptedClaimDatabase([self.deadlock()])
                repository, kwargs = self.repository(name, database)
                claim = repository.claim_next(**kwargs)
                self.assertIsNotNone(claim)
                self.assertEqual(len(database.connections), 2)
                first, second = database.connections
                self.assertIsNot(first, second)
                self.assertEqual(
                    (
                        first.transaction_starts,
                        first.commits,
                        first.rollbacks,
                        first.cursor_closed,
                        first.closed,
                    ),
                    (1, 0, 1, True, True),
                )
                self.assertEqual(
                    (
                        second.transaction_starts,
                        second.commits,
                        second.rollbacks,
                        second.cursor_closed,
                        second.closed,
                    ),
                    (1, 1, 0, True, True),
                )
                attempt_count = (
                    database.raw["processing_attempt_count"]
                    if name == "raw"
                    else database.rich["publish_attempt_count"]
                )
                self.assertEqual(attempt_count, 1)

    def test_second_deadlock_returns_clean_miss_after_two_cleanups(self):
        for name in ("raw", "publication"):
            with self.subTest(repository=name):
                database = ScriptedClaimDatabase([self.deadlock(), self.deadlock()])
                repository, kwargs = self.repository(name, database)
                self.assertIsNone(repository.claim_next(**kwargs))
                self.assertEqual(len(database.connections), 2)
                self.assertIsNot(database.connections[0], database.connections[1])
                for connection in database.connections:
                    self.assertEqual(
                        (
                            connection.transaction_starts,
                            connection.commits,
                            connection.rollbacks,
                            connection.cursor_closed,
                            connection.closed,
                        ),
                        (1, 0, 1, True, True),
                    )
                self.assertEqual(database.raw["processing_attempt_count"], 0)
                self.assertEqual(database.rich["publish_attempt_count"], 0)

    def test_non_deadlock_connector_errors_propagate_after_cleanup(self):
        for name, errno, sqlstate in (
            ("raw", 1205, "HY000"),
            ("publication", 1146, "42S02"),
        ):
            with self.subTest(repository=name, errno=errno):
                error = mysql.connector.errors.DatabaseError(
                    msg="non-contention database failure",
                    errno=errno,
                    sqlstate=sqlstate,
                )
                database = ScriptedClaimDatabase([error])
                repository, kwargs = self.repository(name, database)
                with self.assertRaises(mysql.connector.Error) as raised:
                    repository.claim_next(**kwargs)
                self.assertEqual(raised.exception.errno, errno)
                self.assertEqual(len(database.connections), 1)
                connection = database.connections[0]
                self.assertEqual(
                    (
                        connection.transaction_starts,
                        connection.commits,
                        connection.rollbacks,
                        connection.cursor_closed,
                        connection.closed,
                    ),
                    (1, 0, 1, True, True),
                )


class ProcessingWiringTests(unittest.TestCase):
    def test_durable_path_disabled_preserves_legacy_processing(self):
        connection = Mock()
        cursor = Mock()
        connection.cursor.return_value = cursor
        cursor.fetchall.return_value = [{"id": 1}]
        with (
            patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", False),
            patch.object(gpt_processor, "get_db_connection", return_value=connection),
            patch.object(gpt_processor, "RawNewsRepository") as repository_type,
            patch.object(gpt_processor, "process_one", return_value=True) as process_one,
            patch.object(gpt_processor.logging, "info"),
            patch.object(gpt_processor.logging, "warning"),
        ):
            result = gpt_processor.process_news_with_gpt(batch_size=1)
        self.assertEqual(result, {"attempted": 1, "succeeded": 1, "failed": 0})
        repository_type.assert_not_called()
        process_one.assert_called_once_with({"id": 1})

    def test_durable_path_claims_before_processing_and_completes(self):
        events = []
        repository = Mock()
        repository.claim_next.side_effect = lambda **_kwargs: (
            events.append("claim-committed")
            or ProcessingClaim("a" * 64, {"id": 1}, 1)
        )
        repository.complete.side_effect = lambda *_args: events.append("complete") or True
        with (
            patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", True),
            patch.object(gpt_processor, "RawNewsRepository", return_value=repository),
            patch.object(
                gpt_processor,
                "process_one",
                side_effect=lambda *_args, **_kwargs: events.append("llm") or True,
            ) as process_one,
            patch.object(gpt_processor.logging, "info"),
            patch.object(gpt_processor.logging, "warning"),
            patch.object(gpt_processor.logging, "exception"),
        ):
            result = gpt_processor.process_news_with_gpt(batch_size=1)
        self.assertEqual(events, ["claim-committed", "llm", "complete"])
        self.assertEqual(result["succeeded"], 1)
        self.assertFalse(process_one.call_args.kwargs["mark_complete"])

    def test_processing_exception_marks_claim_retryable(self):
        repository = Mock()
        repository.claim_next.return_value = ProcessingClaim("b" * 64, {"id": 1}, 1)

        def failed(_row, *, on_error, **_kwargs):
            on_error(ValueError("article body"))
            return False

        with (
            patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", True),
            patch.object(gpt_processor, "RawNewsRepository", return_value=repository),
            patch.object(gpt_processor, "process_one", side_effect=failed),
            patch.object(gpt_processor.logging, "info"),
            patch.object(gpt_processor.logging, "warning"),
        ):
            result = gpt_processor.process_news_with_gpt(batch_size=1)
        self.assertEqual(result["failed"], 1)
        repository.fail.assert_called_once()
        self.assertEqual(repository.fail.call_args.args[2], "ValueError: processing failed")

    def test_base_exception_releases_claim_and_reraises(self):
        repository = Mock()
        repository.claim_next.return_value = ProcessingClaim("c" * 64, {"id": 1}, 1)
        with (
            patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", True),
            patch.object(gpt_processor, "RawNewsRepository", return_value=repository),
            patch.object(gpt_processor, "process_one", side_effect=KeyboardInterrupt),
            patch.object(gpt_processor.logging, "info"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                gpt_processor.process_news_with_gpt(batch_size=1)
        repository.release_interrupted.assert_called_once_with(1, "c" * 64)

    def test_durable_store_includes_raw_article_id(self):
        connection = Mock()
        cursor = Mock()
        connection.cursor.return_value = cursor
        record = {
            "title": "Title",
            "full_text": "<p>Body</p>",
            "category": "Markets",
            "hashtags": "Bitcoin",
            "sentiment": "neutral",
        }
        original = {
            "id": 44,
            "news_url": "https://news.example/raw",
            "tickers": [],
        }
        with (
            patch.object(gpt_processor, "PROCESS_DURABLE_CLAIMS_ENABLED", True),
            patch.object(gpt_processor, "get_db_connection", return_value=connection),
        ):
            gpt_processor.store_rich_news(record, original)
        sql, params = cursor.execute.call_args.args
        self.assertIn("raw_article_id", sql)
        self.assertEqual(params[1], 44)


class PublishingRepositoryTests(unittest.TestCase):
    def test_two_publishers_cannot_claim_same_rich_row(self):
        database = FakeClaimDatabase()
        repository = PublicationRepository(connect=database.connect)
        first = repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        second = repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_expired_publisher_claim_can_be_reclaimed(self):
        database = FakeClaimDatabase()
        database.rich.update(
            publish_status="post_created",
            publish_claim_token="old",
            publish_claimed_at="expired",
        )
        claim = PublicationRepository(connect=database.connect).claim_next(
            timeout_minutes=30,
            fresh_start_after=None,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(claim.recovered)
        self.assertNotEqual(claim.token, "old")

    def test_nonexpired_publisher_claim_cannot_be_stolen(self):
        database = FakeClaimDatabase()
        database.rich.update(
            publish_status="claimed",
            publish_claim_token="owner",
            publish_claimed_at="active",
        )
        claim = PublicationRepository(connect=database.connect).claim_next(
            timeout_minutes=30,
            fresh_start_after=None,
        )
        self.assertIsNone(claim)

    def test_only_owner_token_completes_publication(self):
        database = FakeClaimDatabase()
        repository = PublicationRepository(connect=database.connect)
        claim = repository.claim_next(timeout_minutes=30, fresh_start_after=None)
        assert claim is not None
        self.assertFalse(repository.complete(8, "wrong"))
        self.assertTrue(repository.complete(8, claim.token))
        self.assertEqual(database.rich["published"], 1)
        self.assertEqual(database.rich["publish_status"], "published")


class ServiceRepository:
    def __init__(self, row=None):
        self.row = dict(row or ARTICLE)
        self.token = "d" * 64
        self.claimed = False
        self.completed = False
        self.failed = False
        self.events = []
        self.complete_result = True

    def claim_next(self, **_kwargs):
        if self.claimed:
            return None
        self.claimed = True
        return PublicationClaim(self.token, dict(self.row), 1)

    def save_identity(self, rich_id, token, raw_id, key):
        self.events.append("identity-local")
        self.row.update(raw_article_id=raw_id, publication_key=key)
        return token == self.token and rich_id == self.row["id"]

    def save_external_post(self, rich_id, token, post_id, post_url):
        self.events.append("post-local")
        self.row.update(wp_post_id=post_id, wp_post_url=post_url)
        return token == self.token and rich_id == self.row["id"]

    def save_media(self, rich_id, token, media_id, metadata):
        self.events.append("media-local")
        self.row.update(wp_media_id=media_id, wp_media_metadata_json=metadata)
        return token == self.token and rich_id == self.row["id"]

    def clear_media(self, *_args):
        self.row["wp_media_id"] = None
        return True

    def complete(self, *_args):
        self.events.append("complete")
        self.completed = self.complete_result
        return self.complete_result

    def fail(self, *_args):
        self.failed = True
        self.events.append("fail")
        return True

    def release_interrupted(self, *_args):
        self.events.append("release")
        return True


class ClaimingServiceRepository(ServiceRepository):
    def __init__(self, claim_repository):
        super().__init__()
        self.claim_repository = claim_repository

    def claim_next(self, **kwargs):
        claim = self.claim_repository.claim_next(**kwargs)
        if claim is not None:
            self.token = claim.token
            self.row = dict(claim.article)
        return claim


class ServicePublisher:
    def __init__(self):
        self.reconcile_result = None
        self.publish_result = PublicationResult(success=True, external_id=101)
        self.local_media_valid = False
        self.recovered_media = None
        self.published_images = []
        self.events = []

    def reconcile(self, _article, _context):
        self.events.append("reconcile")
        return self.reconcile_result

    def publish(self, _article, image, context):
        self.events.append("publish")
        self.published_images.append(image)
        if self.publish_result.success and self.publish_result.external_id:
            context.persist_external_state(
                self.publish_result.external_id,
                self.publish_result.external_url,
            )
        return self.publish_result

    def external_media_exists(self, _external_id):
        return self.local_media_valid

    def find_external_media(self, _publication_key):
        return self.recovered_media

    def persist_external_media_identity(self, _external_id, _publication_key):
        self.events.append("media-wp-meta")


class PublishingServiceTests(unittest.TestCase):
    def test_transient_claim_deadlock_does_not_end_publish_batch(self):
        database = ScriptedClaimDatabase([ClaimContentionPolicyTests.deadlock()])
        claim_repository = PublicationRepository(connect=database.connect)
        repository = ClaimingServiceRepository(claim_repository)
        adapter = ServicePublisher()

        result = PublishingService(
            repository,
            adapter,
            Mock(return_value=(None, {})),
        ).publish_due(1)

        self.assertEqual(result, {"attempted": 1, "succeeded": 1, "failed": 0})
        self.assertEqual(len(database.connections), 2)
        self.assertIsNot(database.connections[0], database.connections[1])
        self.assertEqual(database.connections[0].rollbacks, 1)
        self.assertTrue(database.connections[0].closed)
        self.assertEqual(database.connections[1].commits, 1)
        self.assertTrue(database.connections[1].closed)
        self.assertTrue(repository.completed)

    def test_publication_key_is_deterministic_and_ignores_title_and_slug(self):
        first = build_publication_key(8, 3, "https://news.example/one")
        changed_title_and_slug = build_publication_key(8, 3, "https://else.example/ignored")
        self.assertEqual(first, "coincourier:8:3")
        self.assertEqual(first, changed_title_and_slug)

    def test_existing_local_media_is_reused_without_image_work(self):
        repository = ServiceRepository({**ARTICLE, "wp_media_id": 77})
        adapter = ServicePublisher()
        adapter.local_media_valid = True
        image_preparer = Mock()
        result = PublishingService(repository, adapter, image_preparer).publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        image_preparer.assert_not_called()
        self.assertEqual(adapter.published_images[0].external_id, 77)

    def test_media_publication_key_recovery_is_saved_and_reused(self):
        repository = ServiceRepository()
        adapter = ServicePublisher()
        adapter.recovered_media = 78
        image_preparer = Mock()
        PublishingService(repository, adapter, image_preparer).publish_due(1)
        image_preparer.assert_not_called()
        self.assertEqual(repository.row["wp_media_id"], 78)
        self.assertEqual(adapter.published_images[0].external_id, 78)

    def test_newly_uploaded_media_gets_external_and_local_identity(self):
        repository = ServiceRepository()
        adapter = ServicePublisher()
        image_preparer = Mock(return_value=(79, {"provider": "openverse"}))
        PublishingService(repository, adapter, image_preparer).publish_due(1)
        self.assertIn("media-wp-meta", adapter.events)
        self.assertIn("media-local", repository.events)
        self.assertEqual(repository.row["wp_media_id"], 79)

    def test_failed_post_does_not_complete_published_state(self):
        repository = ServiceRepository()
        adapter = ServicePublisher()
        adapter.publish_result = PublicationResult(success=False, error="failed")
        result = PublishingService(repository, adapter, Mock(return_value=(None, {}))).publish_due(1)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(repository.completed)
        self.assertTrue(repository.failed)

    def test_reconciliation_finishes_published_state(self):
        repository = ServiceRepository()
        adapter = ServicePublisher()
        adapter.reconcile_result = PublicationResult(
            success=True,
            external_id=101,
            reconciled=True,
        )
        result = PublishingService(repository, adapter, Mock()).publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        self.assertTrue(repository.completed)
        self.assertNotIn("publish", adapter.events)

    def test_successful_new_post_finishes_published_state(self):
        repository = ServiceRepository()
        adapter = ServicePublisher()
        result = PublishingService(repository, adapter, Mock(return_value=(None, {}))).publish_due(1)
        self.assertEqual(result["succeeded"], 1)
        self.assertTrue(repository.completed)
        self.assertEqual(repository.row["wp_post_id"], 101)

    def test_full_claim_token_never_appears_in_service_logs_or_errors(self):
        repository = ServiceRepository()
        adapter = ServicePublisher()
        adapter.publish_result = PublicationResult(success=False, error="authorization token=secret")
        with self.assertLogs("publishing.service", level="INFO") as captured:
            PublishingService(repository, adapter, Mock(return_value=(None, {}))).publish_due(1)
        output = "\n".join(captured.output)
        self.assertIn(claim_prefix(repository.token), output)
        self.assertNotIn(repository.token, output)
        self.assertNotIn("secret", " ".join(str(event) for event in repository.events))


def wp_connection():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False
    return connection


def wp_response(status=201, body=None, text=""):
    response = Mock()
    response.status_code = status
    response.text = text
    response.json.return_value = body or {}
    return response


class WordPressReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.article = PublicationArticle.from_mapping(ARTICLE)
        self.key = build_publication_key(8, 3, ARTICLE["news_url"])

    def context(self, **overrides):
        values = {
            "published_at_utc": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "publication_key": self.key,
            "raw_article_id": 3,
            "rich_article_id": 8,
            "source_url": ARTICLE["news_url"],
        }
        values.update(overrides)
        return PublicationContext(**values)

    def test_local_wp_post_id_prevents_second_post(self):
        callback = Mock()
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection()),
            patch.object(persistence, "find_post_by_id", return_value={"ID": 101, "guid": "url"}),
            patch.object(persistence, "write_publication_metadata"),
            patch.object(publisher, "write_yoast_metadata"),
            patch.object(publisher.session, "post") as post,
        ):
            result = publisher.WordPressPublisher().publish(
                self.article,
                None,
                self.context(existing_external_id=101, persist_external_state=callback),
            )
        self.assertTrue(result.reconciled)
        post.assert_not_called()

    def test_publication_key_lookup_recovers_and_persists_external_id(self):
        callback = Mock()
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection()),
            patch.object(
                persistence,
                "find_post_by_publication_key",
                return_value={"ID": 102, "guid": "https://wp.example/102"},
            ),
            patch.object(persistence, "write_publication_metadata"),
            patch.object(publisher, "write_yoast_metadata"),
            patch.object(publisher.session, "post") as post,
        ):
            result = publisher.WordPressPublisher().publish(
                self.article,
                None,
                self.context(persist_external_state=callback),
            )
        self.assertEqual(result.external_id, 102)
        callback.assert_called_once_with(102, "https://wp.example/102")
        post.assert_not_called()

    def test_local_wp_post_id_with_different_identity_is_rejected(self):
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection()),
            patch.object(
                persistence,
                "find_post_by_id",
                return_value={"ID": 101, "guid": "url", "publication_key": "different"},
            ),
            patch.object(persistence, "write_publication_metadata") as write,
            patch.object(publisher.session, "post") as post,
        ):
            result = publisher.WordPressPublisher().publish(
                self.article,
                None,
                self.context(existing_external_id=101),
            )
        self.assertFalse(result.success)
        write.assert_not_called()
        post.assert_not_called()

    def test_new_post_persists_identity_and_local_id_before_yoast(self):
        events = []
        callback = Mock(side_effect=lambda *_args: events.append("local-id"))
        adapter = publisher.WordPressPublisher()
        with (
            patch.object(adapter, "reconcile", return_value=None),
            patch.object(publisher, "ensure_category", return_value=1),
            patch.object(publisher, "ensure_term", return_value=2),
            patch.object(
                publisher.session,
                "post",
                return_value=wp_response(201, {"id": 103, "link": "https://wp.example/103"}),
            ),
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection()),
            patch.object(
                persistence,
                "write_publication_metadata",
                side_effect=lambda *_args: events.append("wp-meta"),
            ),
            patch.object(
                publisher,
                "write_yoast_metadata",
                side_effect=lambda *_args, **_kwargs: events.append("yoast"),
            ),
            patch("builtins.print"),
        ):
            result = adapter.publish(self.article, None, self.context(persist_external_state=callback))
        self.assertTrue(result.created)
        self.assertEqual(events, ["wp-meta", "local-id", "yoast"])

    def test_local_save_failure_after_201_retries_via_wp_meta_without_second_post(self):
        stored = {}
        callback = Mock(side_effect=[RuntimeError("local unavailable"), None])

        def lookup(_conn, key):
            return stored.get(key)

        def write_meta(_conn, post_id, metadata):
            stored[metadata[persistence.PUBLICATION_KEY_META]] = {
                "ID": post_id,
                "guid": "https://wp.example/104",
            }

        adapter = publisher.WordPressPublisher()
        with (
            patch.object(publisher, "ensure_category", return_value=1),
            patch.object(publisher, "ensure_term", return_value=2),
            patch.object(
                publisher.session,
                "post",
                return_value=wp_response(201, {"id": 104, "link": "https://wp.example/104"}),
            ) as post,
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection()),
            patch.object(persistence, "find_post_by_publication_key", side_effect=lookup),
            patch.object(persistence, "write_publication_metadata", side_effect=write_meta),
            patch.object(publisher, "write_yoast_metadata"),
            patch("builtins.print"),
        ):
            with self.assertRaises(RuntimeError):
                adapter.publish(self.article, None, self.context(persist_external_state=callback))
            result = adapter.publish(self.article, None, self.context(persist_external_state=callback))
        self.assertTrue(result.reconciled)
        self.assertEqual(post.call_count, 1)

    def test_yoast_failure_retries_via_local_id_without_second_post(self):
        saved = {}

        def save_local(post_id, post_url):
            saved.update(id=post_id, url=post_url)

        adapter = publisher.WordPressPublisher()
        with (
            patch.object(publisher, "ensure_category", return_value=1),
            patch.object(publisher, "ensure_term", return_value=2),
            patch.object(
                publisher.session,
                "post",
                return_value=wp_response(201, {"id": 105, "link": "https://wp.example/105"}),
            ) as post,
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection()),
            patch.object(persistence, "find_post_by_publication_key", return_value=None),
            patch.object(persistence, "find_post_by_id", return_value={"ID": 105, "guid": "url"}),
            patch.object(persistence, "write_publication_metadata"),
            patch.object(
                publisher,
                "write_yoast_metadata",
                side_effect=[RuntimeError("yoast unavailable"), None],
            ),
            patch("builtins.print"),
        ):
            with self.assertRaises(RuntimeError):
                adapter.publish(self.article, None, self.context(persist_external_state=save_local))
            result = adapter.publish(
                self.article,
                None,
                self.context(existing_external_id=saved["id"], persist_external_state=save_local),
            )
        self.assertTrue(result.reconciled)
        self.assertEqual(post.call_count, 1)

    def test_publication_metadata_contains_all_coincourier_keys(self):
        with patch.object(persistence, "write_publication_metadata") as write:
            publisher.WordPressPublisher._write_publication_identity(
                Mock(),
                106,
                self.context(),
            )
        metadata = write.call_args.args[2]
        self.assertEqual(
            set(metadata),
            {
                persistence.PUBLICATION_KEY_META,
                persistence.RAW_ARTICLE_ID_META,
                persistence.RICH_ARTICLE_ID_META,
                persistence.SOURCE_URL_META,
            },
        )

    def test_media_publication_key_is_written_through_adapter(self):
        with (
            patch.object(publisher.mysql.connector, "connect", return_value=wp_connection()),
            patch.object(persistence, "write_media_publication_key") as write,
        ):
            publisher.WordPressPublisher().persist_external_media_identity(77, self.key)
        write.assert_called_once_with(unittest.mock.ANY, 77, self.key)

    def test_wordpress_persistence_writes_all_identity_meta_rows(self):
        connection = Mock()
        cursor = Mock()
        cursor.fetchone.return_value = None
        connection.cursor.return_value = cursor
        metadata = {
            persistence.PUBLICATION_KEY_META: self.key,
            persistence.RAW_ARTICLE_ID_META: 3,
            persistence.RICH_ARTICLE_ID_META: 8,
            persistence.SOURCE_URL_META: ARTICLE["news_url"],
        }
        with patch.object(persistence, "get_wp_prefix", return_value="wp_"):
            persistence.write_publication_metadata(connection, 107, metadata)
        inserted_keys = [
            item.args[1][1]
            for item in cursor.execute.call_args_list
            if item.args[0].lstrip().startswith("INSERT")
        ]
        self.assertEqual(inserted_keys, list(metadata))
        self.assertEqual(connection.commit.call_count, 4)


class PublishingWiringTests(unittest.TestCase):
    def test_durable_path_disabled_preserves_phase_one_publisher(self):
        lock = MagicMock()
        lock.is_connected.return_value = True
        adapter = Mock()
        adapter.publish.return_value = PublicationResult(success=True, external_id=101)
        with (
            patch.object(publisher, "PUBLISH_DURABLE_STATE_ENABLED", False),
            patch.object(publisher.mysql.connector, "connect", return_value=lock),
            patch.object(publisher, "_get_lock", return_value=True),
            patch.object(publisher, "_release_lock", return_value=True),
            patch.object(publisher, "_count_due_now", return_value=1),
            patch.object(publisher, "fetch_unpublished", return_value=[ARTICLE]),
            patch.object(publisher, "upload_image", return_value=(None, {})),
            patch.object(publisher, "mark_news_as_published") as mark,
            patch.object(publisher, "WordPressPublisher", return_value=adapter),
            patch.object(publisher, "PublishingService") as service_type,
        ):
            publish_to_wp.publish_news_to_wp()
        service_type.assert_not_called()
        mark.assert_called_once_with(ARTICLE["news_url"])

    def test_durable_path_invokes_service_inside_advisory_lock(self):
        events = []
        lock = MagicMock()
        lock.is_connected.return_value = True
        service = Mock()
        service.publish_due.side_effect = lambda _limit: events.append("service") or {
            "attempted": 1,
            "succeeded": 1,
            "failed": 0,
        }
        with (
            patch.object(publisher, "PUBLISH_DURABLE_STATE_ENABLED", True),
            patch.object(publisher.mysql.connector, "connect", return_value=lock),
            patch.object(publisher, "_get_lock", side_effect=lambda *_args: events.append("lock") or True),
            patch.object(
                publisher,
                "_release_lock",
                side_effect=lambda *_args: events.append("release") or True,
            ),
            patch.object(publisher, "_count_due_now", return_value=1),
            patch.object(publisher, "PublishingService", return_value=service) as service_type,
            patch.object(publisher, "fetch_unpublished") as legacy_fetch,
        ):
            publish_to_wp.publish_news_to_wp()
        self.assertEqual(events, ["lock", "service", "release"])
        service_type.assert_called_once()
        legacy_fetch.assert_not_called()

    def test_compatibility_entry_point_remains_callable(self):
        self.assertIs(publish_to_wp, publisher)
        self.assertTrue(callable(publish_to_wp.publish_news_to_wp))


class MigrationStaticTests(unittest.TestCase):
    def test_migration_is_additive_and_retains_legacy_booleans(self):
        migration = (MIGRATION_DIR / "002_phase2_durable_state.sql").read_text(encoding="utf-8")
        lowered = migration.lower()
        self.assertIn("add column if not exists", lowered)
        self.assertIn("where processed = 1", lowered)
        self.assertIn("where published = 1", lowered)
        self.assertNotIn("drop column", lowered)
        self.assertNotIn("drop table", lowered)
        self.assertNotIn("truncate table", lowered)

    def test_preflight_precedes_unique_indexes_in_documented_order(self):
        readme = (MIGRATION_DIR / "README.md").read_text(encoding="utf-8")
        self.assertLess(
            readme.index("003_phase2_uniqueness_preflight.sql"),
            readme.index("004_phase2_indexes.sql"),
        )
        preflight = (MIGRATION_DIR / "003_phase2_uniqueness_preflight.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("GROUP BY raw_article_id", preflight)
        self.assertIn("GROUP BY publication_key", preflight)
        self.assertIn("GROUP BY wp_post_id", preflight)

    def test_required_claim_and_unique_indexes_exist(self):
        indexes = (MIGRATION_DIR / "004_phase2_indexes.sql").read_text(encoding="utf-8")
        for name in (
            "idx_cryptonewsapi_processing_claim",
            "idx_rich_crpytonews_publish_claim",
            "uq_rich_crpytonews_raw_article_id",
            "uq_rich_crpytonews_publication_key",
            "uq_rich_crpytonews_wp_post_id",
        ):
            self.assertIn(name, indexes)

    def test_rollback_releases_claims_without_deleting_schema(self):
        rollback = (MIGRATION_DIR / "005_phase2_rollback_state.sql").read_text(encoding="utf-8")
        lowered = rollback.lower()
        self.assertIn("processing_claim_token = null", lowered)
        self.assertIn("publish_claim_token = null", lowered)
        self.assertNotIn("drop ", lowered)

    def test_source_defaults_for_durable_flags_are_false(self):
        source = inspect.getsource(config)
        self.assertIn('_env_bool("PROCESS_DURABLE_CLAIMS_ENABLED", False)', source)
        self.assertIn('_env_bool("PUBLISH_DURABLE_STATE_ENABLED", False)', source)
        env_example = (REPOSITORY_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertIn("PROCESS_DURABLE_CLAIMS_ENABLED=false", env_example)
        self.assertIn("PUBLISH_DURABLE_STATE_ENABLED=false", env_example)

    def test_image_search_v1_source_default_is_unchanged(self):
        self.assertIn('os.getenv("IMAGE_SEARCH_ENGINE", "v1")', inspect.getsource(config))


if __name__ == "__main__":
    unittest.main()
