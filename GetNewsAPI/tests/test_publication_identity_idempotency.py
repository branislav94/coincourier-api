"""Offline checks for claimed identity saves with MariaDB changed-row counts."""

from __future__ import annotations

from typing import Any
import unittest

import mysql.connector

from repositories.publication import PublicationRepository


TOKEN = "a" * 64
POST_URL = "https://wordpress.example.invalid/posts/77/"


def owned_identity(**overrides: Any) -> dict[str, Any]:
    """Supply readback independently of the repository's SQL predicates."""
    return {
        "id": 8,
        "published": 0,
        "publish_status": "claimed",
        "publish_claim_token": TOKEN,
        "raw_article_id": 3,
        "publication_key": "coincourier:8:3",
        "wp_post_id": None,
        "wp_post_url": None,
        **overrides,
    }


class ChangedRowsCursor:
    """Return an explicit UPDATE count and independently scripted readback."""

    def __init__(self, connection: ChangedRowsConnection) -> None:
        self.connection = connection
        self.rowcount = 0
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        statement = " ".join(sql.lower().split())
        self.connection.statements.append((statement, params))
        if statement.startswith("update"):
            if self.connection.update_error is not None:
                raise self.connection.update_error
            self.rowcount = self.connection.changed
        elif statement.startswith("select"):
            if self.connection.read_error is not None:
                raise self.connection.read_error
            # SELECT must not erase the captured UPDATE count used for verification.
            self.rowcount = -1
        else:
            raise AssertionError("unexpected claimed identity operation")

    def fetchone(self) -> dict[str, Any] | None:
        return self.connection.readback

    def close(self) -> None:
        self.closed = True


class ChangedRowsConnection:
    """Capture transaction boundaries without database or HTTP connections."""

    def __init__(self, readback: dict[str, Any] | None, *, changed: int = 0) -> None:
        self.readback = readback
        self.changed = changed
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.cursor_instance = ChangedRowsCursor(self)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.update_error: BaseException | None = None
        self.read_error: BaseException | None = None
        self.commit_error: BaseException | None = None
        self.cursor_dictionary: bool | None = None

    def cursor(self, *, dictionary: bool = False) -> ChangedRowsCursor:
        self.cursor_dictionary = dictionary
        return self.cursor_instance

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class ClaimedIdentityIdempotencyTests(unittest.TestCase):
    """Verify both no-op success and the production SQL ownership safeguards."""

    def save(
        self,
        connection: ChangedRowsConnection,
        **expectations: Any,
    ) -> bool:
        return PublicationRepository(connect=lambda: connection).save_identity(
            8, TOKEN, 3, "coincourier:8:3", **expectations,
        )

    def assert_closed(self, connection: ChangedRowsConnection) -> None:
        self.assertTrue(connection.closed)
        self.assertTrue(connection.cursor_instance.closed)

    def assert_owned_sql(self, connection: ChangedRowsConnection) -> None:
        self.assertIs(connection.cursor_dictionary, True)
        update, readback = connection.statements
        update_sql, update_params = update
        read_sql, read_params = readback
        self.assertTrue(update_sql.startswith("update rich_crpytonews"))
        self.assertIn("raw_article_id = coalesce(raw_article_id, %s)", update_sql)
        self.assertIn("publication_key = coalesce(publication_key, %s)", update_sql)
        for sql in (update_sql, read_sql):
            self.assertIn("where id = %s", sql)
            self.assertIn("and publish_claim_token = %s", sql)
            self.assertIn("and published = 0", sql)
            self.assertIn("and publish_status in ('claimed', 'post_created')", sql)
        self.assertIn("and (raw_article_id is null or raw_article_id = %s)", update_sql)
        self.assertIn("and (publication_key is null or publication_key = %s)", update_sql)
        self.assertIn("and wp_post_id <=> %s", update_sql)
        self.assertIn("and wp_post_url <=> %s", update_sql)
        self.assertEqual(
            update_params,
            (3, "coincourier:8:3", 8, TOKEN, 3, "coincourier:8:3", 77, POST_URL),
        )
        self.assertTrue(read_sql.startswith(
            "select id, published, publish_status, publish_claim_token, "
            "raw_article_id, publication_key, wp_post_id, wp_post_url from rich_crpytonews",
        ))
        self.assertTrue(read_sql.endswith("limit 1 for update"))
        self.assertEqual(read_params, (8, TOKEN))

    def test_changed_row_save_verifies_identity_before_commit(self) -> None:
        connection = ChangedRowsConnection(owned_identity(), changed=1)
        self.assertTrue(self.save(connection, expected_post_id=None, expected_post_url=None))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual(len(connection.statements), 2)
        self.assert_closed(connection)

    def test_zero_changed_rows_succeeds_only_for_identical_owned_readback(self) -> None:
        connection = ChangedRowsConnection(owned_identity(
            publish_status="post_created", wp_post_id=77, wp_post_url=POST_URL,
        ), changed=0)
        self.assertTrue(self.save(connection, expected_post_id=77, expected_post_url=POST_URL))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assert_owned_sql(connection)
        self.assert_closed(connection)

    def test_zero_changed_rows_rejects_owner_identity_pointer_and_state_conflicts(self) -> None:
        conflicts = (
            {"id": 9},
            {"publish_claim_token": "b" * 64},
            {"raw_article_id": 4},
            {"publication_key": "coincourier:8:4"},
            {"publication_key": "CoinCourier:8:3"},
            {"wp_post_id": 78},
            {"wp_post_url": "https://wordpress.example.invalid/posts/78/"},
            {"published": 1},
            {"publish_status": "pending"},
            {"publish_status": "retryable"},
            {"publish_status": "published"},
            {"publish_status": None},
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                connection = ChangedRowsConnection(owned_identity(**{
                    "wp_post_id": 77, "wp_post_url": POST_URL, **conflict,
                }))
                self.assertFalse(self.save(connection, expected_post_id=77, expected_post_url=POST_URL))
                self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
                self.assert_owned_sql(connection)
                self.assert_closed(connection)

    def test_missing_owned_readback_cannot_turn_zero_changed_rows_into_success(self) -> None:
        connection = ChangedRowsConnection(None)
        self.assertFalse(self.save(connection, expected_post_id=77, expected_post_url=POST_URL))
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_owned_sql(connection)
        self.assert_closed(connection)

    def test_changed_row_count_does_not_override_conflicting_readback(self) -> None:
        connection = ChangedRowsConnection(owned_identity(raw_article_id=4), changed=1)
        self.assertFalse(self.save(connection, expected_post_id=None, expected_post_url=None))
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_closed(connection)

    def test_explicit_null_pointer_expectations_reject_new_pointer_state(self) -> None:
        for pointer in ({"wp_post_id": 77}, {"wp_post_url": POST_URL}):
            with self.subTest(pointer=pointer):
                connection = ChangedRowsConnection(owned_identity(**pointer))
                self.assertFalse(self.save(connection, expected_post_id=None, expected_post_url=None))
                self.assertEqual(connection.statements[0][1][-2:], (None, None))
                self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
                self.assert_closed(connection)

    def test_omitted_pointer_expectations_preserve_positional_call_compatibility(self) -> None:
        connection = ChangedRowsConnection(owned_identity(wp_post_id=77, wp_post_url=POST_URL))
        self.assertTrue(self.save(connection))
        update_sql, params = connection.statements[0]
        self.assertNotIn("wp_post_id <=>", update_sql)
        self.assertNotIn("wp_post_url <=>", update_sql)
        self.assertEqual(params, (3, "coincourier:8:3", 8, TOKEN, 3, "coincourier:8:3"))
        self.assert_closed(connection)

    def test_unexpected_update_cardinality_is_rejected_even_with_owned_readback(self) -> None:
        connection = ChangedRowsConnection(owned_identity(), changed=2)
        self.assertFalse(self.save(connection, expected_post_id=None, expected_post_url=None))
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_closed(connection)

    def test_database_timeouts_propagate_after_transaction_cleanup(self) -> None:
        for stage in ("update", "read"):
            with self.subTest(stage=stage):
                connection = ChangedRowsConnection(owned_identity())
                error = mysql.connector.Error("synthetic lock timeout", errno=1205)
                setattr(connection, f"{stage}_error", error)
                with self.assertRaises(mysql.connector.Error) as raised:
                    self.save(connection, expected_post_id=None, expected_post_url=None)
                self.assertIs(raised.exception, error)
                self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
                self.assert_closed(connection)

    def test_commit_failure_propagates_after_rollback_and_close(self) -> None:
        connection = ChangedRowsConnection(owned_identity())
        connection.commit_error = RuntimeError("synthetic commit failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic commit failure"):
            self.save(connection, expected_post_id=None, expected_post_url=None)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_closed(connection)

    def test_interrupted_identity_save_rolls_back_and_closes(self) -> None:
        connection = ChangedRowsConnection(owned_identity())
        connection.read_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.save(connection, expected_post_id=None, expected_post_url=None)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_closed(connection)


class LegacyCreationIntentTests(unittest.TestCase):
    """Verify fresh permission becomes a durable attempt before any remote POST."""

    def begin(self, connection: ChangedRowsConnection) -> bool:
        return PublicationRepository(connect=lambda: connection).begin_legacy_creation(8)

    def assert_guarded_sql(self, connection: ChangedRowsConnection) -> None:
        self.assertEqual(len(connection.statements), 1)
        sql, params = connection.statements[0]
        self.assertTrue(sql.startswith("update rich_crpytonews set publish_status = 'claimed',"))
        self.assertIn("publish_attempt_count = 1", sql)
        self.assertIn("publish_claimed_at = utc_timestamp()", sql)
        self.assertIn("where id = %s", sql)
        self.assertIn("and published = 0", sql)
        self.assertIn("and publish_status = 'fresh'", sql)
        self.assertIn("and publish_attempt_count = 0", sql)
        self.assertIn("and wp_post_id is null", sql)
        self.assertIn("and wp_post_url is null", sql)
        self.assertIn("and wp_post_created_at is null", sql)
        self.assertIn("and publish_claim_token is null", sql)
        self.assertNotIn(" or ", sql)
        self.assertEqual(params, (8,))

    def assert_closed(self, connection: ChangedRowsConnection) -> None:
        self.assertTrue(connection.closed)
        self.assertTrue(connection.cursor_instance.closed)

    def test_one_changed_row_commits_fresh_creation_intent(self) -> None:
        connection = ChangedRowsConnection(None, changed=1)
        self.assertTrue(self.begin(connection))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assert_guarded_sql(connection)
        self.assert_closed(connection)

    def test_zero_changed_rows_cannot_authorize_creation(self) -> None:
        connection = ChangedRowsConnection(None, changed=0)
        self.assertFalse(self.begin(connection))
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_guarded_sql(connection)
        self.assert_closed(connection)

    def test_unexpected_update_cardinality_cannot_authorize_creation(self) -> None:
        connection = ChangedRowsConnection(None, changed=2)
        self.assertFalse(self.begin(connection))
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_guarded_sql(connection)
        self.assert_closed(connection)

    def test_invalid_article_ids_stop_before_application_connection(self) -> None:
        connections: list[ChangedRowsConnection] = []

        def connect() -> ChangedRowsConnection:
            connection = ChangedRowsConnection(None, changed=1)
            connections.append(connection)
            return connection

        repository = PublicationRepository(connect=connect)
        for invalid in (None, "8", True, 0, -1, 8.5):
            with self.subTest(article_id=invalid):
                with self.assertRaises(ValueError):
                    repository.begin_legacy_creation(invalid)
        self.assertEqual(connections, [])

    def test_commit_failure_cannot_authorize_creation_and_cleans_transaction(self) -> None:
        connection = ChangedRowsConnection(None, changed=1)
        connection.commit_error = RuntimeError("synthetic creation intent commit failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic creation intent commit failure"):
            self.begin(connection)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_guarded_sql(connection)
        self.assert_closed(connection)

    def test_database_timeout_keeps_existing_error_and_transaction_behavior(self) -> None:
        connection = ChangedRowsConnection(None, changed=1)
        error = mysql.connector.Error("synthetic intent lock timeout", errno=1205)
        connection.update_error = error
        with self.assertRaises(mysql.connector.Error) as raised:
            self.begin(connection)
        self.assertIs(raised.exception, error)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assert_closed(connection)


class ManualReconciliationStateTests(unittest.TestCase):
    """Unresolved rows stop automatic retries without retiring linked posts."""

    def repository(self, connection: ChangedRowsConnection) -> PublicationRepository:
        return PublicationRepository(connect=lambda: connection)

    def assert_manual_transition(self, connection: ChangedRowsConnection) -> str:
        self.assertEqual(len(connection.statements), 1)
        sql = connection.statements[0][0]
        self.assertTrue(sql.startswith("update rich_crpytonews set publish_status = 'manual_review',"))
        self.assertIn("publish_claim_token = null", sql)
        self.assertIn("publish_claimed_at = null", sql)
        self.assertIn("publish_last_error = %s", sql)
        self.assertIn("where id = %s", sql)
        self.assertIn("and published = 0", sql)
        self.assertIn("and wp_post_id is null", sql)
        assignments = sql.split(" where ", 1)[0]
        self.assertNotIn("wp_", assignments)
        self.assertNotIn("raw_article_id", assignments)
        self.assertNotIn("publication_key", assignments)
        self.assertNotIn("publish_attempt_count", assignments)
        self.assertNotIn("published =", assignments)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.cursor_instance.closed)
        return sql

    def test_owned_manual_transition_requires_current_claim_and_missing_post_id(self) -> None:
        connection = ChangedRowsConnection(None, changed=1)
        code = "post_identity_manual_reconciliation_required"
        self.assertTrue(self.repository(connection).require_manual_reconciliation(8, TOKEN, code))
        sql = self.assert_manual_transition(connection)
        self.assertIn("and publish_claim_token = %s", sql)
        self.assertIn("and publish_status in ('claimed', 'post_created')", sql)
        self.assertEqual(connection.statements[0][1], (code, 8, TOKEN))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_no_owned_transition_returns_false_with_unchanged_update_semantics(self) -> None:
        connection = ChangedRowsConnection(None, changed=0)
        self.assertFalse(self.repository(connection).require_manual_reconciliation(
            8, TOKEN, "post_identity_manual_reconciliation_required",
        ))
        self.assert_manual_transition(connection)
        # _owned_update keeps its existing behavior: a zero-row UPDATE commits.
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_owned_manual_diagnostic_is_bounded_to_existing_error_column(self) -> None:
        connection = ChangedRowsConnection(None, changed=1)
        code = "post_identity_manual_reconciliation_required:" + "x" * 600
        self.assertTrue(self.repository(connection).require_manual_reconciliation(8, TOKEN, code))
        self.assertEqual(connection.statements[0][1][0], code[:500])
        self.assertEqual(len(connection.statements[0][1][0]), 500)
        self.assert_manual_transition(connection)

    def test_legacy_manual_transition_handles_historical_null_state_and_preserves_pointer_fields(self) -> None:
        connection = ChangedRowsConnection(None, changed=1)
        self.assertTrue(self.repository(connection).defer_legacy_manual_reconciliation(8))
        sql = self.assert_manual_transition(connection)
        self.assertIn("and (publish_status is null or publish_status <> 'manual_review')", sql)
        self.assertIn("and publish_claim_token is null", sql)
        self.assertEqual(connection.statements[0][1], (
            "post_identity_manual_reconciliation_required", 8,
        ))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_linked_owned_or_already_deferred_legacy_row_returns_false(self) -> None:
        connection = ChangedRowsConnection(None, changed=0)
        self.assertFalse(self.repository(connection).defer_legacy_manual_reconciliation(8))
        sql = self.assert_manual_transition(connection)
        self.assertIn("and publish_claim_token is null", sql)
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_invalid_legacy_article_id_stops_before_application_connection(self) -> None:
        connections: list[ChangedRowsConnection] = []

        def connect() -> ChangedRowsConnection:
            connection = ChangedRowsConnection(None, changed=1)
            connections.append(connection)
            return connection

        repository = PublicationRepository(connect=connect)
        for invalid in (None, "8", True, 0, -1, 8.5):
            with self.subTest(article_id=invalid):
                with self.assertRaises(ValueError):
                    repository.defer_legacy_manual_reconciliation(invalid)
        self.assertEqual(connections, [])


if __name__ == "__main__":
    unittest.main()
