'''Private diagnostic recording with synthetic evidence and no external access.'''

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from GetNewsAPI import article_quality_capture as capture


class ArticleQualityCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.base = Path(self.temporary)
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.manifest = {
            'authorize_dev_capture': True,
            'expires_at': (self.now + timedelta(hours=2)).isoformat(),
            'database_host': 'approved-dev-db.example.invalid',
            'database_name': 'approved_dev_database',
            'wordpress_origin': 'https://approved-dev-wp.example.invalid',
            'approved_raw_ids': [101, 102],
            'private_output_directory': str(self.base / 'private-evidence'),
            'output_not_public': True,
        }
        self.authorization = self.base / 'authorization.json'
        self.environment = {
            capture.ENABLE_ENV: 'true',
            capture.DIRECTORY_ENV: str(self.base / 'private-evidence'),
            capture.AUTHORIZATION_ENV: str(self.authorization),
            'APP_ENV': 'production',
            'OPENAI_API_KEY': 'synthetic-api-secret',
        }
        self.configured = {
            'database_host': self.manifest['database_host'],
            'database_name': self.manifest['database_name'],
            'wordpress_api_url': self.manifest['wordpress_origin'] + '/wp-json/wp/v2',
            'environment': self.environment,
        }

    def make_recorder(self, **overrides):
        self.manifest.update(overrides)
        self.authorization.write_text(json.dumps(self.manifest), encoding='utf-8')
        # Windows deliberately fails closed at runtime. Mock only that platform's
        # private-filesystem proof to exercise portable recording logic locally.
        with patch.object(capture, '_POSIX_PRIVATE_FILES', True), \
                patch.object(capture, '_safe_location', return_value=True), \
                patch.object(capture, '_private_path', return_value=True), \
                patch.object(capture, '_utc_now', return_value=self.now):
            recorder = capture.configure_capture(**self.configured)
        self.assertIsNotNone(recorder)
        return recorder

    def record(self, recorder, stage='rewrite.response', **kwargs):
        arguments = {
            'article_context': {'id': 101}, 'provider': 'grok',
            'model': 'grok_text', 'attempt': 1,
        }
        arguments.update(kwargs)
        with patch.object(capture, '_private_path', return_value=True), \
                patch.object(capture, '_utc_now', return_value=self.now):
            return recorder.record_stage(stage, **arguments)

    def events(self, recorder):
        return [json.loads(path.read_text(encoding='utf-8')) for path in sorted(recorder.directory.glob('*.json'))]

    def test_disabled_default_does_not_inspect_filesystem(self):
        arguments = dict(self.configured, environment={})
        with patch.object(capture, 'Path') as path:
            self.assertIsNone(capture.configure_capture(**arguments))
        path.assert_not_called()

    def test_windows_and_unproven_permissions_fail_closed(self):
        with patch.object(capture, '_POSIX_PRIVATE_FILES', False):
            self.assertIsNone(capture.configure_capture(**self.configured))
        with patch.object(capture, '_POSIX_PRIVATE_FILES', True), \
                patch.object(capture, '_safe_location', return_value=True), \
                patch.object(capture, '_private_path', return_value=False):
            self.assertIsNone(capture.configure_capture(**self.configured))

    def test_private_path_requires_owner_only_real_file_or_directory(self):
        path = MagicMock()
        for mode, owner, expected in (
            (stat.S_IFREG | 0o600, 123, True),
            (stat.S_IFREG | 0o640, 123, False),
            (stat.S_IFREG | 0o600, 456, False),
            (stat.S_IFLNK | 0o600, 123, False),
        ):
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=owner)
            with self.subTest(mode=mode, owner=owner), \
                    patch.object(capture, '_POSIX_PRIVATE_FILES', True), \
                    patch.object(capture.os, 'getuid', return_value=123, create=True):
                self.assertEqual(capture._private_path(path, directory=False), expected)
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=123)
        with patch.object(capture, '_POSIX_PRIVATE_FILES', True), \
                patch.object(capture.os, 'getuid', return_value=123, create=True):
            self.assertTrue(capture._private_path(path, directory=True))

    def test_checkout_and_public_paths_are_rejected(self):
        self.assertFalse(capture._safe_location(capture._REPOSITORY / 'captures'))
        self.assertFalse(capture._safe_location(self.base / 'public' / 'captures'))
        self.assertFalse(capture._safe_location(Path('relative-captures')))

    def test_flat_container_deployment_does_not_treat_filesystem_root_as_checkout(self):
        flat_root = capture._application_root(Path('/app/article_quality_capture.py'))
        self.assertEqual(flat_root, Path('/app'))
        with patch.object(capture, '_REPOSITORY', flat_root), \
                patch.object(capture, '_POSIX_PRIVATE_FILES', False):
            self.assertFalse(capture._safe_location(Path('/app/private-evidence')))
        checkout = Path('/work/coincourier-api/GetNewsAPI/article_quality_capture.py')
        self.assertEqual(capture._application_root(checkout), Path('/work/coincourier-api'))

    def test_exact_destination_authorization_cannot_be_replaced_by_app_env(self):
        self.authorization.write_text(json.dumps(self.manifest), encoding='utf-8')
        cases = (
            {'database_host': 'production.example.invalid'},
            {'database_name': 'production_database'},
            {'wordpress_api_url': 'https://production.example.invalid/wp-json'},
            {'wordpress_api_url': 'https://user:password@approved-dev-wp.example.invalid'},
            {'database_host': None},
        )
        for changed in cases:
            with self.subTest(changed=changed), \
                    patch.object(capture, '_POSIX_PRIVATE_FILES', True), \
                    patch.object(capture, '_safe_location', return_value=True), \
                    patch.object(capture, '_private_path', return_value=True), \
                    patch.object(capture, '_utc_now', return_value=self.now):
                self.assertIsNone(capture.configure_capture(**dict(self.configured, **changed)))
        self.assertIsNotNone(self.make_recorder())

    def test_expired_missing_authorization_and_unbounded_manifests_rejected(self):
        cases = (
            {'authorize_dev_capture': False},
            {'private_output_directory': str(self.base / 'other-evidence')},
            {'output_not_public': False},
            {'expires_at': self.now.isoformat()},
            {'expires_at': (self.now + timedelta(days=2)).isoformat()},
            {'expires_at': '2026-10-16T13:00:00'},
            {'approved_raw_ids': []},
            {'approved_raw_ids': list(range(1, 12))},
            {'approved_raw_ids': [True]},
            {'max_events': 201},
            {'max_total_bytes': 5242881},
            {'max_event_bytes': 65537},
        )
        for changed in cases:
            manifest = dict(self.manifest, **changed)
            self.authorization.write_text(json.dumps(manifest), encoding='utf-8')
            with self.subTest(changed=changed), \
                    patch.object(capture, '_POSIX_PRIVATE_FILES', True), \
                    patch.object(capture, '_safe_location', return_value=True), \
                    patch.object(capture, '_private_path', return_value=True), \
                    patch.object(capture, '_utc_now', return_value=self.now):
                self.assertIsNone(capture.configure_capture(**self.configured))

    def test_stage_metadata_preserves_word_comma_counts_and_body_hash_only(self):
        recorder = self.make_recorder()
        body = '<p>On October 14, 2026, holdings were 1,234 BTC, worth $82,345.67.</p>'
        raw = json.dumps({'title': 'Raw title is excluded', 'full_text': body})
        self.assertTrue(self.record(
            recorder, raw_text=raw, document={'full_text': body, 'title': 'Excluded title'},
            validation_failures=['body_under_450_words'], validation_status='failed',
            finish_reason='length',
        ))
        event = self.events(recorder)[0]
        self.assertEqual(event['schema_version'], 2)
        self.assertEqual(event['stage'], 'rewrite.response')
        self.assertEqual(event['raw_article_id'], 101)
        self.assertEqual(event['provider'], 'grok')
        self.assertEqual(event['model'], 'grok_text')
        self.assertEqual(event['attempt'], 1)
        self.assertEqual(event['timestamp_utc'], self.now.isoformat())
        self.assertEqual(event['response_character_count'], len(raw))
        self.assertEqual(event['punctuation']['comma'], 5)
        self.assertEqual(event['article_word_count'], 13)
        self.assertEqual(event['content_sha256'], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(event['content_hash_scope'], 'article_html')
        self.assertEqual(event['parse_status'], 'parsed')
        self.assertEqual(event['validation_status'], 'failed')
        self.assertEqual(event['validation_failures'], ['body_under_450_words'])
        self.assertNotIn(body, json.dumps(event))
        self.assertNotIn('Excluded title', json.dumps(event))
        self.assertEqual(set(event), {
            'schema_version', 'raw_article_id', 'stage', 'provider', 'model',
            'attempt', 'timestamp_utc', 'expires_at', 'response_character_count',
            'finish_reason', 'validation_status', 'validation_failures',
            'repair_triggers', 'checks', 'parse_status', 'article_word_count',
            'punctuation', 'content_sha256', 'content_hash_scope',
        })

    def test_json_and_href_commas_are_never_reported_as_body_commas(self):
        recorder = self.make_recorder()
        body = '<p>No commas here.</p><a href="https://source.example.invalid/a,b,c">Source</a>'
        self.assertTrue(self.record(
            recorder, raw_text=json.dumps({'title': 'Public title', 'full_text': body}),
        ))
        event = self.events(recorder)[0]
        self.assertEqual(event['punctuation']['comma'], 0)
        self.assertEqual(event['article_word_count'], 4)
        self.assertEqual(event['punctuation']['period'], 1)
        self.assertEqual(event['parse_status'], 'parsed')

    def test_parse_failures_have_no_raw_json_punctuation_metrics(self):
        recorder = self.make_recorder()
        cases = (
            ('A malformed, comma-rich, response', 'invalid_json'),
            ('[]', 'non_object'),
            ('{"title":"No body", "research":"one,two,three"}', 'missing_body'),
            ('{"full_text":null}', 'missing_body'),
            ('{"full_text":123}', 'missing_body'),
            (None, 'not_available'),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                self.assertTrue(self.record(recorder, raw_text=response))
                event = self.events(recorder)[-1]
                self.assertEqual(event['parse_status'], expected)
                self.assertIsNone(event['article_word_count'])
                self.assertIsNone(event['punctuation'])
                self.assertIsNone(event['content_sha256'])
        self.assertTrue(self.record(recorder, document=['not', 'an', 'article']))
        self.assertEqual(self.events(recorder)[-1]['parse_status'], 'non_object')

    def test_stage_sequence_retains_measurements_without_article_contents(self):
        recorder = self.make_recorder()
        stages = (
            ('rewrite.parsed', '<p>One point.</p>', 0),
            ('expansion.parsed', '<p>One point, plus context.</p>', 1),
            ('repair.parsed', '<p>One point, plus context, remains.</p>', 2),
            ('repair.selected_before_cleanup', '<p>One point, plus context, remains.</p>', 2),
            ('repair.post_cleanup', '<p>One point, plus context, remains.</p>', 2),
            ('article.post_cleanup', '<p>One point, plus context, remains.</p>', 2),
            ('article.before_storage', '<p>One point, plus context, remains.</p>', 2),
        )
        for stage, body, _ in stages:
            self.assertTrue(self.record(recorder, stage=stage, document={'full_text': body}))
        events = self.events(recorder)
        self.assertEqual([event['stage'] for event in events], [stage for stage, _, _ in stages])
        self.assertEqual([event['punctuation']['comma'] for event in events], [count for _, _, count in stages])
        self.assertEqual(events[-1]['content_sha256'], events[-2]['content_sha256'])

    def test_pre_repair_checks_exclude_metrics_and_allow_only_symbolic_codes_and_bools(self):
        recorder = self.make_recorder()
        self.assertTrue(self.record(
            recorder, stage='repair.checks',
            checks={
                'status': {
                    'metrics': {'focus': 'SECRET-FOCUS', 'source': 'https://secret.example.invalid'},
                    'checks': {'h2_ok': False, 'at_least_one_external': True,
                               'kw_density_ok': False, 'title_len_ok': 'SECRET-NOT-A-BOOL',
                               'SECRET-CHECK-KEY': 'SECRET-CHECK-VALUE'},
                },
                'selected_repair_triggers': ['h2_ok', 'SECRET-TRIGGER', {'url': 'SECRET'}],
            },
            validation_status='failed',
            validation_failures=['body_under_450_words', 'SECRET-FAILURE'],
            repair_triggers=['at_least_one_external', 'SECRET-OTHER-TRIGGER'],
        ))
        event = self.events(recorder)[0]
        self.assertEqual(event['checks'], {
            'at_least_one_external': True, 'h2_ok': False, 'kw_density_ok': False,
        })
        self.assertEqual(event['repair_triggers'], ['at_least_one_external', 'h2_ok'])
        self.assertEqual(event['validation_failures'], ['body_under_450_words'])
        self.assertNotIn('SECRET', json.dumps(event))
        self.assertNotIn('https://', json.dumps(event))

    def test_uncontrolled_identifiers_and_nonprimitive_values_are_never_stringified(self):
        class DoNotInspect:
            def __str__(self):
                raise AssertionError('stringification is forbidden')

            def __repr__(self):
                raise AssertionError('representation is forbidden')

            def __iter__(self):
                raise AssertionError('traversal is forbidden')

            def __bool__(self):
                raise AssertionError('truthiness is forbidden')

        class StringSubclass(str):
            pass

        recorder = self.make_recorder()
        untrusted = DoNotInspect()
        self.assertFalse(self.record(recorder, stage='https://secret.example.invalid'))
        self.assertFalse(self.record(recorder, stage=untrusted))
        self.assertTrue(self.record(
            recorder, provider=untrusted, model=untrusted, attempt=True,
            request_messages=untrusted, raw_text=untrusted,
            document={'full_text': untrusted, 'ignored': untrusted},
            article_context={'id': 101, 'secret': untrusted},
            finish_reason=untrusted, validation_status=untrusted,
            validation_failures=[untrusted, StringSubclass('body_under_450_words')],
            checks={'status': untrusted, 'h2_ok': 1, 'selected_repair_triggers': untrusted},
            research=untrusted, headers=untrusted, unknown=untrusted,
        ))
        event = self.events(recorder)[0]
        self.assertEqual(event['provider'], 'unknown')
        self.assertEqual(event['model'], 'unknown')
        self.assertEqual(event['finish_reason'], 'unknown')
        self.assertEqual(event['validation_status'], 'not_available')
        self.assertEqual(event['validation_failures'], [])
        self.assertEqual(event['checks'], {})
        self.assertIsNone(event['attempt'])
        self.assertIsNone(event['response_character_count'])
        self.assertEqual(event['parse_status'], 'missing_body')
        self.assertFalse(self.record(recorder, stage=StringSubclass('rewrite.response')))
        self.assertTrue(self.record(
            recorder, provider='https://secret.example.invalid', model='SECRET-MODEL',
            finish_reason='SECRET-FINISH', validation_status='SECRET-STATUS',
        ))
        self.assertNotIn('SECRET', json.dumps(self.events(recorder)))

    def test_signed_urls_and_all_raw_fields_never_reach_files_or_logs(self):
        recorder = self.make_recorder()
        variants = (
            ('sig', 'SENTINEL-SAS-SIGNATURE'),
            ('X-Goog-Signature', 'SENTINEL-GOOGLE-SIGNATURE'),
            ('X-Goog-Credential', 'SENTINEL-GOOGLE-CREDENTIAL'),
            ('jwt', 'SENTINEL-JWT-VALUE'),
            ('sIg', 'SENTINEL-MIXED-CASE'),
            ('%73%69%67', 'SENTINEL%2DENCODED%2DVALUE'),
            ('unknown-auth-value', 'SENTINEL-UNKNOWN-PARAMETER'),
        )
        with self.assertNoLogs(level='DEBUG'):
            for parameter, secret in variants:
                url = 'https://signed.example.invalid/private?' + parameter + '=' + secret
                body = '<p>SENTINEL-ARTICLE-TEXT, dated October 16.</p><a href="' + url + '">Source</a>'
                raw = json.dumps({
                    'title': 'SENTINEL-PLAIN-TITLE', 'full_text': body,
                    'research': 'SENTINEL-RESEARCH ' + url, 'reasoning': 'SENTINEL-HIDDEN',
                })
                self.assertTrue(self.record(
                    recorder, raw_text=raw,
                    article_context={'id': 101, 'news_url': url, 'full_text': body},
                    document={'full_text': body, 'title': 'SENTINEL-PLAIN-TITLE', 'seo_focus': url},
                    request_messages=[{'role': 'user', 'content': 'SENTINEL-REQUEST ' + raw}],
                    supplemental_research='SENTINEL-RESEARCH ' + url,
                    headers={'Authorization': 'SENTINEL-AUTHORIZATION'},
                    cookies='SENTINEL-COOKIE', unknown=url,
                    checks={'secret': url, 'h2_ok': True},
                    validation_failures=['SENTINEL-FAILURE ' + url],
                    finish_reason=url, provider=url, model=url,
                ))
        encoded = ''.join(path.read_text(encoding='utf-8') for path in recorder.directory.glob('*.json'))
        self.assertNotIn('SENTINEL', encoded)
        self.assertNotIn('https://', encoded)
        self.assertNotIn('signed.example.invalid', encoded)
        self.assertNotIn('<p>', encoded)
        self.assertNotIn('research', encoded)
        for _, secret in variants:
            self.assertNotIn(secret, encoded)
        self.assertEqual(len(self.events(recorder)), len(variants))

    def test_only_authorized_ids_and_active_window_are_recorded(self):
        recorder = self.make_recorder()
        for article_id in (None, 999, True, '101'):
            self.assertFalse(self.record(recorder, article_context={'id': article_id}, raw_text='text'))
        with patch.object(capture, '_utc_now', return_value=recorder.expires):
            self.assertFalse(recorder.record_stage(
                'rewrite.response', article_context={'id': 101}, provider='grok', model='grok_text',
            ))
        self.assertEqual(self.events(recorder), [])

    def test_unexpected_provider_content_suppresses_body_metrics_for_only_that_article(self):
        recorder = self.make_recorder()
        body = '<p>Public facts, dated October 16.</p>'
        self.assertTrue(self.record(recorder, suppress_evidence=True, raw_text='SENTINEL-REASONING'))
        self.assertTrue(self.record(recorder, stage='rewrite.parsed', document={'full_text': body}))
        self.assertTrue(self.record(recorder, stage='repair.parsed', suppress_evidence=False,
                                   document={'full_text': body}))
        self.assertTrue(self.record(recorder, article_context={'id': 102}, document={'full_text': body}))
        events = self.events(recorder)
        for event in events[:3]:
            self.assertTrue(event['evidence_incomplete_unexpected_provider_content'])
            self.assertEqual(event['parse_status'], 'suppressed')
            self.assertIsNone(event['punctuation'])
            self.assertIsNone(event['content_sha256'])
        self.assertEqual(events[3]['punctuation']['comma'], 1)
        self.assertNotIn('evidence_incomplete_unexpected_provider_content', events[3])
        self.assertNotIn('SENTINEL', json.dumps(events))

    def test_unapproved_id_cannot_expand_suppression_state(self):
        recorder = self.make_recorder()
        self.assertFalse(self.record(recorder, article_context={'id': 999}, suppress_evidence=True))
        self.assertEqual(recorder._suppressed_ids, set())

    def test_large_unknown_inputs_are_ignored_and_metadata_size_is_bounded(self):
        recorder = self.make_recorder()
        self.assertTrue(self.record(
            recorder, request_messages='SENTINEL' * 100000,
            supplemental_research='SENTINEL' * 100000,
            validation_failures=['body_under_450_words'] * 10000,
            checks={'h2_ok': False, 'ignored': 'SENTINEL' * 100000},
        ))
        event = self.events(recorder)[0]
        self.assertEqual(event['validation_failures'], ['body_under_450_words'])
        self.assertEqual(event['checks'], {'h2_ok': False})
        self.assertLess(next(recorder.directory.glob('*.json')).stat().st_size, 2000)
        self.assertNotIn('SENTINEL', json.dumps(event))

    def test_event_and_total_byte_budgets_do_not_reset_each_stage(self):
        recorder = self.make_recorder(max_events=1)
        self.assertTrue(self.record(recorder))
        self.assertFalse(self.record(recorder, stage='expansion.response'))
        self.assertEqual(len(self.events(recorder)), 1)
        other = capture.CaptureRecorder(
            recorder.directory, frozenset({101}), recorder.expires, (100, 1, 65536),
        )
        self.assertFalse(self.record(other))
        self.assertEqual(len(self.events(recorder)), 1)
        too_small = capture.CaptureRecorder(
            recorder.directory, frozenset({101}), recorder.expires, (100, 65536, 1),
        )
        self.assertFalse(self.record(too_small))
        self.assertEqual(len(self.events(recorder)), 1)

    def test_exclusive_file_creation_requests_owner_only_mode(self):
        recorder = self.make_recorder()
        original_open = capture.os.open
        with patch.object(capture.os, 'open', wraps=original_open) as open_file:
            self.assertTrue(self.record(recorder))
        args = open_file.call_args.args
        self.assertEqual(args[2], 0o600)
        self.assertTrue(args[1] & capture.os.O_EXCL)

    def test_io_failure_is_a_no_op_without_body_logging(self):
        recorder = self.make_recorder()
        with patch.object(capture.os, 'open', side_effect=OSError('SENTINEL-IO-SECRET')), \
                self.assertNoLogs(level='DEBUG'):
            self.assertFalse(self.record(recorder, raw_text='SENTINEL-ARTICLE'))
        self.assertEqual(self.events(recorder), [])
        broken = MagicMock()
        broken.record_stage.side_effect = RuntimeError('SENTINEL-ERROR-SECRET')
        self.assertFalse(capture.record_stage('rewrite.response', recorder=broken))
        self.assertFalse(capture.record_stage('rewrite.response'))


if __name__ == '__main__':
    unittest.main()
