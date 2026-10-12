'''Offline regression checks for the legacy application publication pointer.'''

from __future__ import annotations

import unittest
from typing import Any

from publishing.models import is_fresh_publication_state
from repositories.publication import PublicationRepository


class FakeIdentityDatabase:
    '''Keep a committed pointer and simulate isolated application transactions.'''

    def __init__(self, **overrides: Any) -> None:
        self.row: dict[str, Any] = {
            'id': 8,
            'published': 0,
            'wp_post_id': None,
            'wp_post_url': None,
            'wp_post_created_at': None,
            'publish_status': 'fresh',
            'publish_attempt_count': 0,
            'publish_claim_token': None,
            'publish_claimed_at': None,
            'private_article_body': 'excluded from identity projection',
            **overrides,
        }
        self.missing_columns = False
        self.commit_error = False
        self.readback_override: dict[str, Any] | None = None
        self.connections: list[FakeIdentityConnection] = []
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def connect(self) -> FakeIdentityConnection:
        connection = FakeIdentityConnection(self)
        self.connections.append(connection)
        return connection


class FakeIdentityConnection:
    '''Expose transaction effects without a database or runtime configuration.'''

    def __init__(self, database: FakeIdentityDatabase) -> None:
        self.database = database
        self.pending = dict(database.row)
        self.cursor_instance: FakeIdentityCursor | None = None
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, *, dictionary: bool = False) -> FakeIdentityCursor:
        if not dictionary:
            raise AssertionError('identity queries must return a named projection')
        self.cursor_instance = FakeIdentityCursor(self)
        return self.cursor_instance

    def commit(self) -> None:
        if self.database.commit_error:
            raise RuntimeError('synthetic application commit failure')
        self.database.row = dict(self.pending)
        self.commits += 1

    def rollback(self) -> None:
        self.pending = dict(self.database.row)
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeIdentityCursor:
    '''Simulate row ownership, pointer conflicts and idempotent affected rows.'''

    def __init__(self, connection: FakeIdentityConnection) -> None:
        self.connection = connection
        self.rowcount = 0
        self.result: dict[str, Any] | None = None
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        database = self.connection.database
        if database.missing_columns:
            raise RuntimeError('synthetic missing application pointer columns')
        normalized = ' '.join(sql.lower().split())
        database.queries.append((normalized, params))
        row = self.connection.pending
        if normalized.startswith('select'):
            rich_id = params[0]
            if row['id'] != rich_id or row['published'] != 0:
                self.result = None
            elif database.readback_override is not None:
                self.result = dict(database.readback_override)
            else:
                projection = normalized.split(' from ', 1)[0].removeprefix('select ')
                self.result = {key.strip(): row[key.strip()] for key in projection.split(',')}
            return
        if normalized.startswith('update') and "set publish_status = 'claimed'" in normalized:
            rich_id, = params
            if (row['id'] == rich_id and row['published'] == 0
                    and row['publish_status'] == 'fresh' and row['publish_attempt_count'] == 0
                    and row['publish_claim_token'] is None
                    and all(row[key] is None for key in ('wp_post_id', 'wp_post_url', 'wp_post_created_at'))):
                row.update(publish_status='claimed', publish_attempt_count=1, publish_claimed_at='active')
                self.rowcount = 1
            return
        if normalized.startswith('update'):
            post_id, post_url, rich_id, expected_id = params
            owned = (
                row['id'] == rich_id
                and row['published'] == 0
                and row['wp_post_id'] in (None, expected_id)
            )
            before = dict(row)
            if owned:
                row['wp_post_id'] = post_id
                if post_url is not None:
                    row['wp_post_url'] = post_url
            self.rowcount = int(row != before)
            return
        raise AssertionError('unexpected identity operation')

    def fetchone(self) -> dict[str, Any] | None:
        return self.result

    def close(self) -> None:
        self.closed = True


class LegacyPublicationIdentityTests(unittest.TestCase):
    '''Ensure metadata retries use a persistent application-owned post pointer.'''

    def setUp(self) -> None:
        self.database = FakeIdentityDatabase()
        self.repository = PublicationRepository(connect=self.database.connect)

    def assert_connection_closed(self) -> None:
        connection = self.database.connections[-1]
        self.assertTrue(connection.closed)
        self.assertIsNotNone(connection.cursor_instance)
        self.assertTrue(connection.cursor_instance.closed)

    def test_identity_preflight_exports_only_publication_state(self) -> None:
        self.assertEqual(
            self.repository.get_external_post_state(8),
            {key: self.database.row[key] for key in (
                'id', 'published', 'publish_status', 'publish_attempt_count', 'publish_claim_token',
                'wp_post_id', 'wp_post_url', 'wp_post_created_at',
            )},
        )
        self.assert_connection_closed()

    def test_missing_article_stops_identity_preflight(self) -> None:
        with self.assertRaisesRegex(RuntimeError, 'identity is unavailable'):
            self.repository.get_external_post_state(9)
        self.assertEqual(self.database.connections[-1].rollbacks, 1)
        self.assert_connection_closed()

    def test_already_published_article_stops_identity_preflight(self) -> None:
        self.database.row['published'] = 1
        with self.assertRaisesRegex(RuntimeError, 'identity is unavailable'):
            self.repository.get_external_post_state(8)
        self.assert_connection_closed()

    def test_missing_pointer_columns_surface_before_remote_creation(self) -> None:
        self.database.missing_columns = True
        with self.assertRaisesRegex(RuntimeError, 'missing application pointer columns'):
            self.repository.get_external_post_state(8)
        self.assertEqual(self.database.connections[-1].rollbacks, 1)
        self.assert_connection_closed()

    def test_saved_pointer_is_available_to_a_new_repository_instance(self) -> None:
        self.assertTrue(self.repository.save_legacy_external_post(8, 5479, 'https://dev.example/5479'))
        restarted = PublicationRepository(connect=self.database.connect)
        self.assertEqual(
            restarted.get_external_post_state(8),
            {key: self.database.row[key] for key in (
                'id', 'published', 'publish_status', 'publish_attempt_count', 'publish_claim_token',
                'wp_post_id', 'wp_post_url', 'wp_post_created_at',
            )},
        )
        self.assertEqual(self.database.connections[0].commits, 1)

    def test_conflicting_pointer_is_preserved_and_failure_is_visible(self) -> None:
        self.database.row.update(wp_post_id=5477, wp_post_url='original URL')
        self.assertFalse(self.repository.save_legacy_external_post(8, 5479, 'replacement URL'))
        self.assertEqual(self.database.row['wp_post_id'], 5477)
        self.assertEqual(self.database.row['wp_post_url'], 'original URL')
        self.assertEqual(self.database.connections[-1].commits, 0)
        self.assertEqual(self.database.connections[-1].rollbacks, 1)

    def test_identical_save_succeeds_with_zero_affected_rows(self) -> None:
        self.database.row.update(wp_post_id=5479, wp_post_url='same URL')
        self.assertTrue(self.repository.save_legacy_external_post(8, 5479, 'same URL'))
        self.assertEqual(self.database.connections[-1].cursor_instance.rowcount, 0)
        self.assertEqual(self.database.connections[-1].commits, 1)

    def test_same_post_identity_can_refresh_its_url(self) -> None:
        self.database.row.update(wp_post_id=5479, wp_post_url='old URL')
        self.assertTrue(self.repository.save_legacy_external_post(8, 5479, 'new URL'))
        self.assertEqual(self.database.row['wp_post_url'], 'new URL')

    def test_absent_new_url_preserves_the_existing_url(self) -> None:
        self.database.row.update(wp_post_id=5479, wp_post_url='existing URL')
        self.assertTrue(self.repository.save_legacy_external_post(8, 5479, None))
        self.assertEqual(self.database.row['wp_post_url'], 'existing URL')

    def test_completed_article_pointer_cannot_be_changed(self) -> None:
        self.database.row.update(published=1, wp_post_id=5479, wp_post_url='existing URL')
        self.assertFalse(self.repository.save_legacy_external_post(8, 5479, 'replacement URL'))
        self.assertEqual(self.database.row['wp_post_url'], 'existing URL')

    def test_mismatched_id_readback_rolls_back_the_pointer(self) -> None:
        self.database.readback_override = {'id': 8, 'wp_post_id': 5477, 'wp_post_url': 'new URL'}
        self.assertFalse(self.repository.save_legacy_external_post(8, 5479, 'new URL'))
        self.assertIsNone(self.database.row['wp_post_id'])
        self.assertEqual(self.database.connections[-1].rollbacks, 1)

    def test_mismatched_url_readback_rolls_back_the_pointer(self) -> None:
        self.database.readback_override = {'id': 8, 'wp_post_id': 5479, 'wp_post_url': 'wrong URL'}
        self.assertFalse(self.repository.save_legacy_external_post(8, 5479, 'intended URL'))
        self.assertIsNone(self.database.row['wp_post_id'])
        self.assertEqual(self.database.connections[-1].rollbacks, 1)

    def test_commit_failure_is_raised_and_transaction_is_rolled_back(self) -> None:
        self.database.commit_error = True
        with self.assertRaisesRegex(RuntimeError, 'application commit failure'):
            self.repository.save_legacy_external_post(8, 5479, 'new URL')
        self.assertIsNone(self.database.row['wp_post_id'])
        self.assertEqual(self.database.connections[-1].rollbacks, 1)
        self.assert_connection_closed()

    def test_invalid_identity_stops_before_database_access(self) -> None:
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.repository.get_external_post_state(invalid)
                with self.assertRaises(ValueError):
                    self.repository.save_legacy_external_post(8, invalid, None)
        self.assertEqual(self.database.connections, [])


    def test_pointer_sql_preserves_article_owner_and_existing_identity(self) -> None:
        self.assertTrue(self.repository.save_legacy_external_post(8, 5479, 'https://dev.example/5479'))
        update, params = self.database.queries[0]
        self.assertIn('where id = %s', update)
        self.assertIn('and published = 0', update)
        self.assertIn('and (wp_post_id is null or wp_post_id = %s)', update)
        self.assertIn('wp_post_url = coalesce(%s, wp_post_url)', update)
        self.assertEqual(params, (5479, 'https://dev.example/5479', 8, 5479))
        readback, params = self.database.queries[1]
        self.assertIn('where id = %s and published = 0', readback)
        self.assertEqual(params, (8,))

class PublicationFreshnessTests(unittest.TestCase):
    def test_only_explicit_unattempted_fresh_state_allows_creation(self) -> None:
        row = FakeIdentityDatabase().row
        self.assertTrue(is_fresh_publication_state(row))
        cases = [
            {'publish_status': 'pending'}, {'publish_status': 'retryable'},
            {'publish_status': 'claimed'}, {'publish_status': 'post_created'},
            {'publish_status': None}, {'publish_attempt_count': 1},
            {'publish_attempt_count': False}, {'publish_attempt_count': '0'},
            {'published': 1}, {'wp_post_id': 77}, {'wp_post_url': 'https://dev.example/77'},
            {'wp_post_created_at': '2026-10-12 00:00:00'}, {'publish_claim_token': 'owner'},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertFalse(is_fresh_publication_state({**row, **changes}))

    def test_missing_provenance_fields_fail_closed(self) -> None:
        row = FakeIdentityDatabase().row
        for field in ('published', 'publish_status', 'publish_attempt_count', 'wp_post_id',
                      'wp_post_url', 'wp_post_created_at', 'publish_claim_token'):
            with self.subTest(field=field):
                incomplete = dict(row)
                incomplete.pop(field)
                self.assertFalse(is_fresh_publication_state(incomplete))


if __name__ == '__main__':
    unittest.main()
