'''Focused WordPress/Yoast metadata regressions using only fake boundaries.'''

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

import requests

from publishing import PublicationArticle, PublicationContext
from publishing.wordpress import publisher


class FakeResponse:
    '''Represent a local HTTP response without network behavior.'''

    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.body = body
        self.status_code = status
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self) -> dict[str, Any]:
        '''Return the synthetic response payload.'''
        return self.body


class FakeWordPress:
    '''Capture native REST create/update/read requests and simulate metadata outcomes.'''

    def __init__(self, outcome: str = 'accepted') -> None:
        self.outcome = outcome
        self.creates: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.stored_meta: dict[str, str] = {
            '_yoast_wpseo_focuskw': 'Existing focus',
            '_yoast_wpseo_title': 'Existing SEO title',
            '_yoast_wpseo_metadesc': 'Existing description',
        }

    def _reply(self, post_id: int) -> dict[str, Any]:
        '''Expose accepted, stale, missing or incorrect response metadata explicitly.'''
        body: dict[str, Any] = {
            'id': 88 if self.outcome == 'wrong_id' else post_id,
            'link': f'https://wordpress.example.invalid/posts/{post_id}/',
            'type': 'post', 'status': 'publish',
        }
        if self.outcome != 'missing_meta':
            body['meta'] = dict(self.stored_meta)
        return body

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        '''Create or update a synthetic post; raise only the requested fake timeout.'''
        data = json.loads(json.dumps(kwargs.get('json', {}), ensure_ascii=False))
        if url.rstrip('/').endswith('/posts'):
            post_id = 77 + len(self.creates)
            self.creates.append({'url': url, 'json': data, 'id': post_id})
            self.events.append(f'create:{post_id}')
            return FakeResponse({'id': post_id, 'link': f'https://wordpress.example.invalid/posts/{post_id}/'}, 201)
        post_id = int(url.rstrip('/').rsplit('/', 1)[1])
        self.updates.append({'url': url, 'json': data, 'id': post_id, 'timeout': kwargs.get('timeout')})
        self.events.append(f'meta:{post_id}')
        if self.outcome == 'timeout':
            raise requests.ReadTimeout('Synthetic metadata response timeout')
        if self.outcome == 'rejected':
            return FakeResponse({'code': 'rest_cannot_update', 'message': 'Protected field rejected'}, 403)
        if self.outcome not in {'mismatch', 'missing_meta', 'wrong_id'}:
            self.stored_meta.update(data.get('meta', {}))
        return FakeResponse(self._reply(post_id))

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        '''Read one synthetic post without changing its state.'''
        self.reads.append({'url': url, **kwargs})
        return FakeResponse(self._reply(int(url.rstrip('/').rsplit('/', 1)[1])))


class YoastMetadataReadbackTests(unittest.TestCase):
    '''Accept only the expected description's WordPress text-field whitespace form.'''

    PRODUCTION_EXPECTED = (
        'The recent  Strive bitcoin buy  stands out as one of the largest single acquisitions '
        'in recent months. Strive Inc. purchased 3,311 BTC for roughly 280'
    )
    PRODUCTION_ACTUAL = (
        'The recent Strive bitcoin buy stands out as one of the largest single acquisitions '
        'in recent months. Strive Inc. purchased 3,311 BTC for roughly 280'
    )

    def setUp(self) -> None:
        '''Use the confirmed production values without any database or HTTP connection.'''
        self.expected = {
            '_yoast_wpseo_focuskw': 'strive bitcoin buy',
            '_yoast_wpseo_title': 'Strive Bitcoin Buy Dwarfs Strategy Purchase',
            '_yoast_wpseo_metadesc': self.PRODUCTION_EXPECTED,
        }

    def _verify(self, metadata: dict[str, Any]) -> None:
        '''Exercise the same independent readback verifier used by the REST writer.'''
        publisher.wordpress_seo._verify_response(
            FakeResponse({'id': 5303, 'meta': metadata}), 5303, self.expected, 'readback',
        )

    def test_exact_match_succeeds_without_canonical_comparison(self) -> None:
        '''Exact values remain the first successful comparison path for every field.'''
        with patch.object(publisher.wordpress_seo.re, 'sub',
                          side_effect=AssertionError('Exact equality must pass immediately')):
            for description in (self.PRODUCTION_EXPECTED, ' \tExact\r\n text\x00\x0b '):
                with self.subTest(description=description):
                    self.expected['_yoast_wpseo_metadesc'] = description
                    self._verify(dict(self.expected))

    def test_production_space_readback_succeeds_without_changing_intended_post(self) -> None:
        '''Accept the 150-to-148-character production pair and retain all original write values.'''
        self.assertEqual(len(self.PRODUCTION_EXPECTED), 150)
        self.assertEqual(len(self.PRODUCTION_ACTUAL), 148)
        actual = {**self.expected, '_yoast_wpseo_metadesc': self.PRODUCTION_ACTUAL}
        http = FakeWordPress()
        with patch.object(http, 'get', return_value=FakeResponse({'id': 5303, 'meta': actual})):
            publisher.wordpress_seo.write_yoast_metadata(
                http, 5303, focus_keyword=self.expected['_yoast_wpseo_focuskw'],
                description=self.PRODUCTION_EXPECTED, title=self.expected['_yoast_wpseo_title'],
                api_base='https://wordpress.example.invalid', timeout=(10.0, 60.0),
            )
        self.assertEqual(http.updates[0]['json'], {'meta': self.expected})
        self.assertEqual(http.stored_meta, self.expected)
        self.assertEqual(self.expected['_yoast_wpseo_metadesc'], self.PRODUCTION_EXPECTED)
        self.assertFalse(http.creates)

    def test_confirmed_production_whitespace_excerpts_succeed_without_mutation(self) -> None:
        '''Use the supplied excerpts for rows 490/552/607, not invented full descriptions.'''
        excerpts = {
            'rich_490_wp_5441_suffix': ('... The specific — bitcoin ', '... The specific — bitcoin'),
            'rich_552_wp_5624_excerpt': (
                'Ripple Prime  has launched ... October 15, 2026. \n The products',
                'Ripple Prime has launched ... October 15, 2026. The products',
            ),
            'rich_607_wp_5789_suffix': ('... ParaFi Investor Day. ', '... ParaFi Investor Day.'),
        }
        for case, (expected, actual) in excerpts.items():
            with self.subTest(case=case):
                self.expected['_yoast_wpseo_metadesc'] = expected
                before = dict(self.expected)
                http = FakeWordPress()
                readback = {**before, '_yoast_wpseo_metadesc': actual}
                with patch.object(http, 'get', return_value=FakeResponse({'id': 5303, 'meta': readback})):
                    publisher.wordpress_seo.write_yoast_metadata(
                        http, 5303, focus_keyword=before['_yoast_wpseo_focuskw'],
                        description=expected, title=before['_yoast_wpseo_title'],
                        api_base='https://wordpress.example.invalid', timeout=(10.0, 60.0),
                    )
                self.assertEqual(http.updates[0]['json'], {'meta': before})
                self.assertEqual(http.stored_meta, before)
                self.assertEqual(self.expected, before)

    def test_expected_ascii_text_field_runs_and_trim_succeed(self) -> None:
        '''Collapse CR/LF/TAB/SPACE runs and use PHP's default trim character set only.'''
        descriptions = ['A  report', 'A\treport', 'A\nreport', 'A\rreport',
                        'A \r\n\t  report']
        descriptions.extend(character + 'A report' + character
                            for character in (' ', '\t', '\n', '\r', '\x00', '\x0b'))
        descriptions.append(' \t\n\r\x00\x0bA \r\n\t  report\x0b\x00\r\n\t ')
        for description in descriptions:
            with self.subTest(expected=description):
                self.expected['_yoast_wpseo_metadesc'] = description
                self._verify({**self.expected, '_yoast_wpseo_metadesc': 'A report'})

    def test_material_description_differences_still_fail(self) -> None:
        '''Space tolerance cannot mask truncation, numbers, words, ordering or punctuation.'''
        actual = self.PRODUCTION_ACTUAL
        variants = {
            'changed_number': actual.replace('3,311', '3,312'),
            'truncated': actual[:-4],
            'missing_word': actual.replace('single acquisitions', 'acquisitions'),
            'additional_word': actual.replace('bitcoin buy', 'bitcoin market buy'),
            'reordered_words': actual.replace('largest single', 'single largest'),
            'changed_punctuation': actual.replace('months.', 'months!'),
            'changed_meaning': actual.replace('purchased', 'sold'),
            'material_content': 'A different report about Strive bitcoin purchases.',
        }
        for difference, description in variants.items():
            with self.subTest(difference=difference):
                with self.assertRaisesRegex(
                    publisher.wordpress_seo.MetadataSyncError,
                    'metadata_readback_value_mismatch:_yoast_wpseo_metadesc',
                ):
                    self._verify({**self.expected, '_yoast_wpseo_metadesc': description})

    def test_actual_whitespace_and_unproven_text_representations_still_fail(self) -> None:
        '''Never normalize actual readback, other whitespace, Unicode or HTML entities.'''
        variants = {
            'actual_repeated_space': ('A report', 'A  report'),
            'actual_longer_space_run': ('A  report', 'A   report'),
            'actual_tab': ('A  report', 'A\treport'),
            'actual_linefeed': ('A  report', 'A\nreport'),
            'actual_carriage_return': ('A  report', 'A\rreport'),
            'nbsp': ('A  report', 'A\u00a0report'),
            'expected_nbsp': ('A\u00a0report', 'A report'),
            'expected_nbsp_boundary': ('\u00a0A report\u00a0', 'A report'),
            'em_space': ('A  report', 'A\u2003report'),
            'expected_em_space': ('A\u2003report', 'A report'),
            'expected_em_space_boundary': ('\u2003A report\u2003', 'A report'),
            'thin_space': ('A  report', 'A\u2009report'),
            'zero_width_space': ('A  report', 'A\u200breport'),
            'expected_formfeed': ('A\x0creport', 'A report'),
            'expected_formfeed_boundary': ('\x0cA report\x0c', 'A report'),
            'expected_interior_vertical_tab': ('A\x0breport', 'A report'),
            'expected_interior_nul': ('A\x00report', 'A report'),
            'leading_addition': ('A  report', ' A report'),
            'trailing_addition': ('A  report', 'A report '),
            'actual_vertical_tab_boundary': ('A report', '\x0bA report\x0b'),
            'actual_nul_boundary': ('A report', '\x00A report\x00'),
            'unicode_nfc': ('Caf\u00e9  report', 'Cafe\u0301 report'),
            'html_entity': ('A  &amp; report', 'A & report'),
        }
        for difference, (expected, actual) in variants.items():
            with self.subTest(difference=difference):
                self.expected['_yoast_wpseo_metadesc'] = expected
                with self.assertRaisesRegex(
                    publisher.wordpress_seo.MetadataSyncError,
                    'metadata_readback_value_mismatch:_yoast_wpseo_metadesc',
                ):
                    self._verify({**self.expected, '_yoast_wpseo_metadesc': actual})

    def test_non_string_description_readback_still_fails(self) -> None:
        '''The comparison fallback never coerces missing or non-string values.'''
        for actual in (None, 0, True, [], {}):
            with self.subTest(actual=actual):
                with self.assertRaisesRegex(
                    publisher.wordpress_seo.MetadataSyncError,
                    'metadata_readback_value_mismatch:_yoast_wpseo_metadesc',
                ):
                    self._verify({**self.expected, '_yoast_wpseo_metadesc': actual})

    def test_title_and_focus_keep_exact_comparison(self) -> None:
        '''Title/focus whitespace differences remain failures in either direction.'''
        variants = [('A  report', 'A report'), ('A\treport', 'A report'),
                    ('A\nreport', 'A report'), ('A\rreport', 'A report'),
                    (' A report', 'A report'), ('A report ', 'A report'),
                    ('\x00A report\x0b', 'A report'), ('A\u00a0report', 'A report')]
        for key in ('_yoast_wpseo_title', '_yoast_wpseo_focuskw'):
            for expected, readback in variants:
                for expected, readback in ((expected, readback), (readback, expected)):
                    with self.subTest(field=key, expected=expected, actual=readback):
                        self.expected[key] = expected
                        actual = {**self.expected, '_yoast_wpseo_metadesc': self.PRODUCTION_ACTUAL,
                                  key: readback}
                        with self.assertRaisesRegex(
                            publisher.wordpress_seo.MetadataSyncError,
                            f'metadata_readback_value_mismatch:{key}',
                        ):
                            self._verify(actual)


class YoastMetadataPublicationTests(unittest.TestCase):
    '''Verify publication mapping, round trips and partial success independently of SQL.'''

    def setUp(self) -> None:
        '''Replace every runtime boundary before invoking the existing publisher.'''
        self.http = FakeWordPress()
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.connection.__exit__.return_value = False
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(publisher, 'session', self.http))
        self.stack.enter_context(patch.object(publisher, 'WP_API_URL', 'https://wordpress.example.invalid'))
        self.stack.enter_context(patch.object(publisher, 'ensure_category', return_value=12))
        self.stack.enter_context(patch.object(publisher, 'ensure_term', return_value=13))
        self.stack.enter_context(patch.object(publisher, 'get_wp_prefix', return_value='wp_'))
        self.db_connect = self.stack.enter_context(
            patch.object(publisher.mysql.connector, 'connect', return_value=self.connection),
        )
        self.stack.enter_context(patch.object(publisher.wordpress_persistence, 'find_post_by_id',
                                            return_value={'ID': 77, 'guid': 'https://wordpress.example.invalid/posts/77/',
                                                          'publication_key': 'coincourier:11:22'}))
        self.stack.enter_context(patch.object(publisher.wordpress_persistence, 'find_post_by_publication_key',
                                            return_value=None))
        self.stack.enter_context(patch.object(publisher.wordpress_persistence, 'write_publication_metadata'))
        self.article = PublicationArticle.from_mapping({
            'title': 'Éther’s recovery & the headline ending must survive SEO metadata publication',
            'full_text': '<p>A factual report for readers.</p>',
            'category': 'Markets',
            'seo_focus': 'Éther’s recovery',
            'seo_meta': 'Éther’s recovery: O\'Brien says “verify” & preserve the full description. ' + 'Detail ' * 20 + r' "ledger" path \facts\ledger',
            'seo_slug': 'ether-recovery',
        })
        self.context = PublicationContext(
            published_at_utc=datetime(2026, 10, 12, tzinfo=timezone.utc), creation_is_fresh=True,
        )

    def _publish(self, context: PublicationContext | None = None) -> Any:
        '''Call the unchanged public publication API with synthetic inputs.'''
        return publisher.WordPressPublisher().publish(self.article, None, context or self.context)

    def _metadata(self) -> dict[str, Any]:
        '''Require a native REST metadata request rather than silently accepting SQL.'''
        self.assertTrue(self.http.updates, 'Yoast fields never reached the native REST post endpoint')
        return self.http.updates[-1]['json']['meta']

    def test_rich_fields_map_to_full_native_yoast_metadata(self) -> None:
        '''Preserve focus, article-derived SEO title and complete description without clipping.'''
        result = self._publish()
        self.assertTrue(result.success)
        self.assertEqual(self._metadata(), {
            '_yoast_wpseo_focuskw': self.article.seo_focus,
            '_yoast_wpseo_title': self.article.title,
            '_yoast_wpseo_metadesc': self.article.seo_description,
        })
        self.assertEqual(self.http.creates[0]['json']['title'], self.article.title)

    def test_description_is_separate_from_native_excerpt(self) -> None:
        '''A meta description must not become a native excerpt implicitly.'''
        self.assertTrue(self._publish().success)
        self.assertEqual(self._metadata()['_yoast_wpseo_metadesc'], self.article.seo_description)
        self.assertNotEqual(self.http.creates[0]['json'].get('excerpt'), self.article.seo_description)

    def test_unicode_apostrophes_and_ampersands_survive_json_handoff(self) -> None:
        '''Round-trip supported text and punctuation through actual JSON serialization.'''
        self.assertTrue(self._publish().success)
        sent = json.loads(json.dumps(self._metadata(), ensure_ascii=False))
        self.assertEqual(sent['_yoast_wpseo_focuskw'], 'Éther’s recovery')
        self.assertEqual(sent['_yoast_wpseo_metadesc'], self.article.seo_description)
        self.assertIn('O\'Brien', sent['_yoast_wpseo_metadesc'])
        self.assertIn('“verify” &', sent['_yoast_wpseo_metadesc'])
        self.assertIn(r'"ledger" path \facts\ledger', sent['_yoast_wpseo_metadesc'])

    def test_missing_or_empty_seo_values_do_not_clear_existing_metadata(self) -> None:
        '''Reject absent or whitespace values visibly before any create or metadata mutation.'''
        for focus, description in [(None, None), ('', ''), ('  ', '\t'),
                                   ('Valid focus', None), ('Valid focus', '  '),
                                   (None, 'Valid description')]:
            with self.subTest(focus=focus, description=description):
                self.article = replace(self.article, seo_focus=focus, seo_description=description)
                result = self._publish()
                self.assertFalse(result.success)
                self.assertTrue(result.error)
                self.assertFalse(self.http.creates)
                self.assertFalse(self.http.updates)
                self.assertEqual(self.http.stored_meta['_yoast_wpseo_focuskw'], 'Existing focus')
                self.assertEqual(self.http.stored_meta['_yoast_wpseo_metadesc'], 'Existing description')

    def test_separate_seo_title_and_excerpt_preserve_distinct_fields(self) -> None:
        '''Native title, explicit excerpt and supported optional SEO title have separate owners.'''
        self.article = PublicationArticle.from_mapping({
            'title': 'Native Éther headline', 'full_text': '<p>Factual body.</p>',
            'category': 'Markets', 'seo_focus': 'Éther outlook',
            'seo_meta': 'Yoast description for O\'Brien & readers.',
            'seo_title': 'Explicit SEO title for Éther',
            'excerpt': 'An independently written native excerpt.',
        })
        self.assertTrue(self._publish().success)
        native = self.http.creates[0]['json']
        self.assertEqual(native['title'], 'Native Éther headline')
        self.assertEqual(native.get('excerpt'), 'An independently written native excerpt.')
        self.assertEqual(self._metadata()['_yoast_wpseo_title'], 'Explicit SEO title for Éther')
        self.assertEqual(self._metadata()['_yoast_wpseo_metadesc'], 'Yoast description for O\'Brien & readers.')
        self.assertNotEqual(native['excerpt'], self._metadata()['_yoast_wpseo_metadesc'])

    def test_custom_canonical_remains_unchanged_while_supported_metadata_succeeds(self) -> None:
        '''Ignore an unsupported canonical write without blocking the three confirmed fields.'''
        self.article = replace(self.article, canonical_url='https://source.example.invalid/original')
        self.http.stored_meta['_yoast_wpseo_canonical'] = 'https://wordpress.example.invalid/existing/'
        with self.assertLogs(publisher.wordpress_seo.logger, level='WARNING') as captured:
            result = self._publish()
        self.assertTrue(result.success)
        self.assertEqual(set(self._metadata()), {
            '_yoast_wpseo_focuskw', '_yoast_wpseo_title', '_yoast_wpseo_metadesc',
        })
        self.assertEqual(self.http.stored_meta['_yoast_wpseo_canonical'],
                         'https://wordpress.example.invalid/existing/')
        self.assertEqual(self._metadata()['_yoast_wpseo_metadesc'], self.article.seo_description)
        self.assertIn('unsupported-rest-field', '\n'.join(captured.output))
        self.assertIn('77', '\n'.join(captured.output))
        self.db_connect.assert_not_called()

    def test_protected_metadata_rejection_retains_created_post_id(self) -> None:
        '''An HTTP rejection must remain a partial publication failure with the known identity.'''
        self.http.outcome = 'rejected'
        result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertTrue(result.created)
        self.assertTrue(result.error)
        self.assertEqual(len(self.http.creates), 1)

    def test_successful_transport_with_absent_metadata_is_not_seo_success(self) -> None:
        '''A 200 response missing protected fields cannot verify delivery.'''
        self.http.outcome = 'missing_meta'
        result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertTrue(result.error)

    def test_successful_transport_with_stale_metadata_is_not_seo_success(self) -> None:
        '''A 200 response containing stale fields must fail the round-trip check.'''
        self.http.outcome = 'mismatch'
        result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertTrue(result.error)

    def test_readback_mismatch_after_accepted_write_is_not_seo_success(self) -> None:
        '''An echoed successful write still needs an independently matching stored value.'''
        stale = dict(self.http.stored_meta)
        with patch.object(self.http, 'get', return_value=FakeResponse({'id': 77, 'meta': stale})):
            result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertIn('readback', result.error)
        self.assertTrue(self.http.updates)
        self.assertEqual(self.http.stored_meta['_yoast_wpseo_metadesc'], self.article.seo_description)

    def test_readback_rejection_after_accepted_write_is_not_seo_success(self) -> None:
        '''Write capability alone does not establish that stored metadata can be verified.'''
        with patch.object(self.http, 'get', return_value=FakeResponse({'code': 'rest_forbidden'}, 403)):
            result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertIn('readback', result.error)
        self.assertTrue(self.http.updates)

    def test_readback_missing_metadata_after_accepted_write_is_not_seo_success(self) -> None:
        '''A successful write cannot conceal absent protected fields in the separate read.'''
        with patch.object(self.http, 'get', return_value=FakeResponse({'id': 77})):
            result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertIn('readback', result.error)
        self.assertTrue(self.http.updates)

    def test_readback_wrong_post_id_after_accepted_write_is_not_seo_success(self) -> None:
        '''Matching field values on another post cannot verify this publication identity.'''
        expected = {'_yoast_wpseo_focuskw': self.article.seo_focus,
                    '_yoast_wpseo_title': self.article.title,
                    '_yoast_wpseo_metadesc': self.article.seo_description}
        with patch.object(self.http, 'get', return_value=FakeResponse({'id': 88, 'meta': expected})):
            result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertIn('readback', result.error)
        self.assertTrue(self.http.updates)

    def test_metadata_readback_uses_authenticated_context_and_bounded_timeout(self) -> None:
        '''Inspect only this post's identity and metadata using the existing authenticated session.'''
        self.assertTrue(self._publish().success)
        self.assertEqual(len(self.http.reads), 1)
        request = self.http.reads[0]
        self.assertEqual(request['url'], 'https://wordpress.example.invalid/wp-json/wp/v2/posts/77')
        self.assertEqual(request['params'], {'context': 'edit', '_fields': 'id,meta'})
        self.assertEqual(request['timeout'], publisher.wordpress_client.WP_HTTP_TIMEOUT)
        self.assertEqual(self.http.updates[0]['timeout'], publisher.wordpress_client.WP_HTTP_TIMEOUT)

    def test_wrong_metadata_response_post_id_is_rejected(self) -> None:
        '''The metadata response must refer to the created post, preserving its known ID.'''
        self.http.outcome = 'wrong_id'
        result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertTrue(result.error)

    def test_saved_post_without_publication_key_reuses_owning_rest_post(self) -> None:
        '''A durable known ID remains sufficient for the owning REST retry.'''
        result = self._publish(replace(self.context, existing_external_id=77))
        self.assertTrue(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertTrue(result.reconciled)
        self.assertFalse(self.http.creates)
        self.assertTrue(self.http.updates)
        self.db_connect.assert_not_called()

    def test_metadata_delivery_has_no_wordpress_sql_prefix_dependency(self) -> None:
        '''Metadata delivery for a new non-durable post uses WordPress runtime hooks only.'''
        self.assertTrue(self._publish().success)
        self.db_connect.assert_not_called()
        self.connection.cursor.assert_not_called()

    def test_created_identity_is_persisted_before_metadata_failure_and_retried_without_create(self) -> None:
        '''Preserve known post identity before a timeout; retry metadata on that same post.'''
        saved: list[tuple[int, str | None]] = []

        def persist(post_id: int, post_url: str | None) -> None:
            '''Record local state without any database mutation.'''
            saved.append((post_id, post_url))
            self.http.events.append(f'persist:{post_id}')

        context = replace(self.context, publication_key='coincourier:11:22', raw_article_id=22,
                          rich_article_id=11, persist_external_state=persist)
        self.http.outcome = 'timeout'
        first = self._publish(context)
        self.assertFalse(first.success)
        self.assertEqual(first.external_id, 77)
        self.assertEqual(saved[0][0], 77)
        self.assertLess(self.http.events.index('persist:77'), self.http.events.index('meta:77'))
        self.http.outcome = 'accepted'
        second = self._publish(replace(context, existing_external_id=first.external_id))
        self.assertTrue(second.success)
        self.assertEqual(second.external_id, 77)
        self.assertEqual(len(self.http.creates), 1)
        self.assertEqual({item['id'] for item in self.http.updates}, {77})

    def test_entire_metadata_values_survive_real_json_serialization(self) -> None:
        '''Keep colons, dollars, non-breaking spaces and every field distinct.'''
        self.article = PublicationArticle.from_mapping({
            'title': 'Native headline: $42 for O\'Brien’s Éther',
            'full_text': '<p>Content must never replace the supplied description.</p>',
            'category': 'Markets',
            'seo_focus': 'Éther\u00a0outlook',
            'seo_title': 'SEO: O\'Brien’s “Éther” costs $42\u00a0today',
            'seo_meta': 'Description: O\'Brien’s “Éther” costs $42\u00a0today; 東京 & Zurich. '
                        + 'The final sentence must remain complete. ' * 6,
            'excerpt': 'A native excerpt with a separate purpose.',
        })
        self.assertTrue(self._publish().success)
        serialized = json.loads(json.dumps(self.http.updates[-1]['json'], ensure_ascii=False))
        self.assertEqual(serialized['meta'], {
            '_yoast_wpseo_focuskw': 'Éther\u00a0outlook',
            '_yoast_wpseo_title': 'SEO: O\'Brien’s “Éther” costs $42\u00a0today',
            '_yoast_wpseo_metadesc': self.article.seo_description,
        })
        self.assertGreater(len(serialized['meta']['_yoast_wpseo_metadesc']), 160)
        self.assertEqual(self.http.stored_meta, serialized['meta'])
        native = self.http.creates[0]['json']
        self.assertEqual(native['title'], 'Native headline: $42 for O\'Brien’s Éther')
        self.assertEqual(native['excerpt'], 'A native excerpt with a separate purpose.')
        self.assertNotEqual(native['title'], serialized['meta']['_yoast_wpseo_title'])
        self.assertNotEqual(native['excerpt'], serialized['meta']['_yoast_wpseo_metadesc'])

    def test_independent_readback_absent_description_logs_post_and_field(self) -> None:
        '''A present meta object with only metadesc absent still fails verification.'''
        incomplete = {
            '_yoast_wpseo_focuskw': self.article.seo_focus,
            '_yoast_wpseo_title': self.article.title,
        }
        with (
            patch.object(self.http, 'get', return_value=FakeResponse({'id': 77, 'meta': incomplete})),
            self.assertLogs(publisher.logger, level='ERROR') as captured,
        ):
            result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertEqual(result.error, 'metadata_readback_missing_field:_yoast_wpseo_metadesc')
        logs = '\n'.join(captured.output)
        self.assertIn('77', logs)
        self.assertIn('_yoast_wpseo_metadesc', logs)
        self.assertNotIn(self.article.seo_description, logs)
        self.assertEqual(self.http.stored_meta['_yoast_wpseo_metadesc'], self.article.seo_description)

    def test_auth_rejections_preserve_identity_and_sanitize_remote_error_text(self) -> None:
        '''401 and 403 expose controlled status codes without logging response secrets.'''
        secret = 'Synthetic bearer-token=must-not-appear-in-logs'
        for status in (401, 403):
            with self.subTest(status=status):
                self.http = FakeWordPress()
                original_post = self.http.post

                def rejecting_post(url: str, **kwargs: Any) -> FakeResponse:
                    '''Reject only the metadata write with deliberately sensitive text.'''
                    if url.rstrip('/').endswith('/posts'):
                        return original_post(url, **kwargs)
                    return FakeResponse({'code': 'rest_cannot_update', 'message': secret}, status)

                with (
                    patch.object(publisher, 'session', self.http),
                    patch.object(self.http, 'post', side_effect=rejecting_post),
                    self.assertLogs(publisher.logger, level='ERROR') as captured,
                ):
                    result = self._publish()
                self.assertFalse(result.success)
                self.assertTrue(result.created)
                self.assertEqual(result.external_id, 77)
                self.assertEqual(result.error, f'metadata_write_http_status:{status}')
                logs = '\n'.join(captured.output)
                self.assertIn('77', logs)
                self.assertIn(f'metadata_write_http_status:{status}', logs)
                self.assertNotIn(secret, logs)
                self.assertNotIn(secret, result.error)
                self.assertEqual(len(self.http.creates), 1)
                self.assertFalse(self.http.reads)

    def test_saved_post_reconciliation_uses_owning_rest_without_wordpress_sql(self) -> None:
        '''Retry a durable known ID on the actual owning API even when WP SQL is unavailable.'''
        context = replace(self.context, existing_external_id=77, publication_key='coincourier:11:22',
                          rich_article_id=11, raw_article_id=22)
        with (
            patch.object(publisher.mysql.connector, 'connect',
                         side_effect=RuntimeError('Synthetic stale WordPress DB must not be used')) as sql,
            patch.object(publisher.wordpress_persistence, 'write_publication_metadata') as identity,
        ):
            result = self._publish(context)
        self.assertTrue(result.success)
        self.assertTrue(result.reconciled)
        self.assertEqual(result.external_id, 77)
        self.assertFalse(self.http.creates)
        self.assertTrue(self.http.reads)
        self.assertTrue(all(request['url'].endswith('/posts/77') for request in self.http.reads))
        self.assertEqual({request['id'] for request in self.http.updates}, {77})
        sql.assert_not_called()
        identity.assert_not_called()

    def test_saved_post_rest_auth_failure_never_creates_a_replacement(self) -> None:
        '''An unavailable known post remains an explicit failure rather than triggering create.'''
        context = replace(self.context, existing_external_id=77, publication_key='coincourier:11:22',
                          rich_article_id=11, raw_article_id=22)
        with (
            patch.object(self.http, 'get', return_value=FakeResponse({'code': 'rest_forbidden'}, 403)),
            patch.object(publisher.mysql.connector, 'connect',
                         side_effect=RuntimeError('Synthetic stale WordPress DB must not be used')) as sql,
        ):
            result = self._publish(context)
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertTrue(result.error)
        self.assertFalse(self.http.creates)
        self.assertFalse(self.http.updates)
        sql.assert_not_called()

    def test_successful_write_echo_can_omit_or_alter_meta_when_independent_storage_matches(self) -> None:
        '''Storage verification comes from the independent GET rather than the write echo.'''
        for outcome in ('omit', 'stale'):
            with self.subTest(outcome=outcome):
                self.http = FakeWordPress()
                original_post = self.http.post

                def altered_echo(url: str, **kwargs: Any) -> FakeResponse:
                    '''Persist the requested values but omit or stale only the write response.'''
                    response = original_post(url, **kwargs)
                    if url.rstrip('/').endswith('/posts'):
                        return response
                    body = dict(response.body)
                    if outcome == 'omit':
                        body.pop('meta')
                    else:
                        body['meta'] = {'_yoast_wpseo_metadesc': 'An unreliable write echo.'}
                    return FakeResponse(body)

                with (
                    patch.object(publisher, 'session', self.http),
                    patch.object(self.http, 'post', side_effect=altered_echo),
                ):
                    result = self._publish()
                self.assertTrue(result.success)
                self.assertEqual(result.external_id, 77)
                self.assertEqual(len(self.http.reads), 1)
                self.assertEqual(self.http.stored_meta, {
                    '_yoast_wpseo_focuskw': self.article.seo_focus,
                    '_yoast_wpseo_title': self.article.title,
                    '_yoast_wpseo_metadesc': self.article.seo_description,
                })

    def test_wrong_write_response_id_is_rejected_after_independent_readback(self) -> None:
        '''Even a matching separate GET cannot justify a write response for a different post.'''
        original_post = self.http.post

        def wrong_identity_echo(url: str, **kwargs: Any) -> FakeResponse:
            '''Change only the synthetic write response identity.'''
            response = original_post(url, **kwargs)
            if url.rstrip('/').endswith('/posts'):
                return response
            return FakeResponse({**response.body, 'id': 88})

        with patch.object(self.http, 'post', side_effect=wrong_identity_echo):
            result = self._publish()
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertEqual(result.error, 'metadata_write_post_id_mismatch')
        self.assertEqual(len(self.http.reads), 1)
        self.assertEqual(self.http.stored_meta['_yoast_wpseo_metadesc'], self.article.seo_description)


    def test_known_post_recovery_rejects_invalid_rest_identity_without_mutations(self) -> None:
        cases = [
            ('wrong_id', FakeResponse({'id': 88, 'type': 'post', 'status': 'publish',
                                      'link': 'https://wordpress.example.invalid/posts/77/'}),
             'post_reconcile_id_mismatch'),
            ('wrong_type', FakeResponse({'id': 77, 'type': 'attachment', 'status': 'publish',
                                        'link': 'https://wordpress.example.invalid/posts/77/'}),
             'post_reconcile_invalid_post'),
            ('trashed', FakeResponse({'id': 77, 'type': 'post', 'status': 'trash',
                                     'link': 'https://wordpress.example.invalid/posts/77/'}),
             'post_reconcile_invalid_post'),
            ('missing', FakeResponse({'code': 'rest_post_invalid_id'}, 404),
             'post_reconcile_http_status:404'),
            ('foreign_site', FakeResponse({'id': 77, 'type': 'post', 'status': 'publish',
                                          'link': 'https://another.example.invalid/posts/77/'}),
             'post_reconcile_site_mismatch'),
            ('invalid_payload', FakeResponse([]), 'post_reconcile_id_mismatch'),
        ]
        for label, response, code in cases:
            with self.subTest(case=label):
                persist = MagicMock()
                context = replace(self.context, existing_external_id=77,
                                  persist_external_state=persist, creation_is_fresh=False)
                with patch.object(self.http, 'get', return_value=response):
                    result = self._publish(context)
                self.assertFalse(result.success)
                self.assertEqual(result.external_id, 77)
                self.assertEqual(result.error, code)
                self.assertFalse(self.http.creates)
                self.assertFalse(self.http.updates)
                persist.assert_not_called()
        self.db_connect.assert_not_called()

    def test_known_post_invalid_json_and_captured_foreign_url_never_create(self) -> None:
        persist = MagicMock()
        context = replace(self.context, existing_external_id=77,
                          persist_external_state=persist, creation_is_fresh=False)
        response = FakeResponse({})
        with (
            patch.object(self.http, 'get', return_value=response),
            patch.object(response, 'json', side_effect=ValueError('synthetic invalid JSON')),
        ):
            result = self._publish(context)
        self.assertEqual(result.error, 'post_reconcile_invalid_json')
        self.assertEqual(result.external_id, 77)
        with patch.object(self.http, 'get') as get:
            result = self._publish(replace(context, existing_external_url='https://another.example.invalid/77'))
        self.assertEqual(result.error, 'post_reconcile_site_mismatch')
        self.assertEqual(result.external_id, 77)
        get.assert_not_called()
        persist.assert_not_called()
        self.assertFalse(self.http.creates)
        self.assertFalse(self.http.updates)

    def test_missing_title_and_invalid_saved_ids_never_create(self) -> None:
        original = self.article
        for title in (None, '', '  '):
            with self.subTest(title=title):
                self.article = replace(original, seo_title=title if title is not None else '', title='')
                result = self._publish()
                self.assertEqual(result.error, 'metadata_missing_value:_yoast_wpseo_title')
                self.assertFalse(self.http.creates)
                self.assertFalse(self.http.updates)
        self.article = original
        for post_id in (0, -1, True, '77'):
            with self.subTest(post_id=post_id):
                result = self._publish(replace(self.context, existing_external_id=post_id))
                self.assertEqual(result.error, 'post_reconcile_invalid_id')
                self.assertFalse(self.http.creates)
                self.assertFalse(self.http.updates)

    def test_creation_requires_explicit_fresh_permission(self) -> None:
        for permission in (False, None, 1, 'true'):
            with self.subTest(permission=permission):
                result = self._publish(replace(self.context, creation_is_fresh=permission))
                self.assertFalse(result.success)
                self.assertEqual(result.error, 'post_identity_manual_reconciliation_required')
                self.assertFalse(self.http.creates)
                self.assertFalse(self.http.updates)

    def test_creation_intent_failure_never_creates_a_post(self) -> None:
        intent = MagicMock(side_effect=RuntimeError('synthetic creation-intent commit failure'))
        result = self._publish(replace(self.context, persist_creation_intent=intent))
        self.assertFalse(result.success)
        self.assertEqual(result.error, 'post_creation_intent_persist:RuntimeError')
        intent.assert_called_once_with()
        self.assertFalse(self.http.creates)
        self.assertFalse(self.http.updates)

    def test_invalid_metadata_keeps_known_post_identity_and_url(self) -> None:
        self.article = replace(self.article, seo_description=None)
        url = 'https://wordpress.example.invalid/posts/77/'
        result = self._publish(replace(self.context, existing_external_id=77, existing_external_url=url))
        self.assertFalse(result.success)
        self.assertEqual(result.external_id, 77)
        self.assertEqual(result.external_url, url)
        self.assertEqual(result.error, 'metadata_missing_value:_yoast_wpseo_metadesc')
        self.assertFalse(self.http.creates)
        self.assertFalse(self.http.updates)
        self.assertFalse(self.http.reads)


class LegacyPublicationRetryTests(unittest.TestCase):
    '''Exercise the real legacy orchestrator with persisted fake application pointers.'''

    def test_legacy_metadata_failure_retries_identity_without_second_create(self) -> None:
        '''Reload a persisted application ID before image work on the next legacy attempt.'''
        http = FakeWordPress('rejected')
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        connection.is_connected.return_value = True
        state: dict[str, Any] = {
            'id': 11, 'published': 0, 'publish_status': 'fresh', 'publish_attempt_count': 0,
            'publish_claim_token': None, 'wp_post_id': None, 'wp_post_url': None,
            'wp_post_created_at': None,
        }
        row = {
            'id': 11, 'durable_raw_article_id': 22,
            'title': 'A native article headline', 'full_text': '<p>Factual body.</p>',
            'category': 'Markets', 'seo_focus': 'market news',
            'seo_meta': 'A truthful description for the article.',
            'news_url': 'https://source.example.invalid/news',
        }
        repository = MagicMock()

        def store(rich_id: int, post_id: int, post_url: str | None) -> bool:
            '''Persist a pointer in the fake application backend before metadata work.'''
            self.assertEqual(rich_id, 11)
            state.update(wp_post_id=post_id, wp_post_url=post_url)
            return True

        def lookup(rich_id: int) -> dict[str, Any]:
            '''Return newly loaded state instead of stale generated-row contents.'''
            self.assertEqual(rich_id, 11)
            return dict(state)

        def begin_creation(rich_id: int) -> bool:
            self.assertEqual(rich_id, 11)
            self.assertEqual(state['publish_status'], 'fresh')
            state.update(publish_status='claimed', publish_attempt_count=1)
            return True

        repository.begin_legacy_creation.side_effect = begin_creation
        repository.get_external_post_state.side_effect = lookup
        repository.save_legacy_external_post.side_effect = store
        with (
            patch.object(publisher, 'PUBLISH_DURABLE_STATE_ENABLED', False),
            patch.object(publisher, 'session', http),
            patch.object(publisher, 'WP_API_URL', 'https://wordpress.example.invalid'),
            patch.object(publisher.mysql.connector, 'connect', return_value=connection),
            patch.object(publisher, 'PublicationRepository', return_value=repository),
            patch.object(publisher, '_get_lock', return_value=True),
            patch.object(publisher, '_release_lock', return_value=True),
            patch.object(publisher, '_count_due_now', return_value=1),
            patch.object(publisher, 'fetch_unpublished', return_value=[row]),
            patch.object(publisher, 'upload_image', return_value=(None, {})) as image,
            patch.object(publisher, 'ensure_category', return_value=12),
            patch.object(publisher, 'ensure_term', return_value=13),
            patch.object(publisher.wordpress_persistence, 'find_post_by_publication_key') as marker_lookup,
            patch.object(publisher.wordpress_persistence, 'write_publication_metadata') as marker_write,
            patch.object(publisher, 'mark_news_as_published') as complete,
        ):
            first = publisher.publish_news_to_wp()
            self.assertEqual(first['failed'], 1)
            complete.assert_not_called()
            self.assertEqual(state['wp_post_id'], 77)
            self.assertEqual(len(http.creates), 1)
            http.outcome = 'accepted'
            second = publisher.publish_news_to_wp()
        self.assertEqual(second['succeeded'], 1)
        self.assertEqual(second['failed'], 0)
        self.assertEqual(len(http.creates), 1)
        self.assertEqual({request['id'] for request in http.updates}, {77})
        complete.assert_called_once_with(row['news_url'])
        self.assertEqual(image.call_count, 1)
        self.assertEqual(repository.get_external_post_state.call_count, 2)
        repository.begin_legacy_creation.assert_called_once_with(11)
        marker_lookup.assert_not_called()
        marker_write.assert_not_called()

    def test_legacy_pointer_commit_failure_consumes_fresh_permission(self) -> None:
        from repositories.publication import PublicationRepository

        http = FakeWordPress()
        lock = MagicMock()
        lock.is_connected.return_value = True
        with tempfile.TemporaryDirectory(prefix='legacy-publication-fixture-') as directory:
            path = str(Path(directory) / 'app.sqlite')
            create_restart_database(path)
            repository = PublicationRepository(connect=lambda: SQLitePublicationConnection(
                path, fail_post_identity_commit=True,
            ))
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                row = dict(connection.execute('SELECT * FROM rich_crpytonews WHERE id=11').fetchone())
            with (
                patch.object(publisher, 'PUBLISH_DURABLE_STATE_ENABLED', False),
                patch.object(publisher, 'session', http),
                patch.object(publisher, 'WP_API_URL', 'https://wordpress.example.invalid'),
                patch.object(publisher.mysql.connector, 'connect', return_value=lock),
                patch.object(publisher, 'PublicationRepository', return_value=repository),
                patch.object(publisher, '_get_lock', return_value=True),
                patch.object(publisher, '_release_lock', return_value=True),
                patch.object(publisher, '_count_due_now', return_value=1),
                patch.object(publisher, 'fetch_unpublished', return_value=[row]),
                patch.object(publisher, 'upload_image', return_value=(None, {})) as image,
                patch.object(publisher, 'ensure_category', return_value=12),
                patch.object(publisher, 'ensure_term', return_value=13),
                patch.object(publisher, 'mark_news_as_published') as complete,
            ):
                first = publisher.publish_news_to_wp()
                state = repository.get_external_post_state(11)
                self.assertEqual(first['failed'], 1)
                self.assertIsNone(state['wp_post_id'])
                self.assertEqual(state['publish_status'], 'claimed')
                self.assertEqual(state['publish_attempt_count'], 1)
                with self.assertLogs(publisher.logger, level='ERROR') as captured:
                    second = publisher.publish_news_to_wp()
            self.assertEqual(second['failed'], 1)
            self.assertEqual(repository.get_external_post_state(11)['publish_status'], 'manual_review')
            self.assertIn('post_identity_manual_reconciliation_required', '\n'.join(captured.output))
            self.assertEqual(len(http.creates), 1)
            self.assertFalse(http.updates)
            self.assertEqual(image.call_count, 1)
            complete.assert_not_called()


class SQLitePublicationCursor:
    '''Translate the repository's narrow SQL dialect for an isolated local fixture.'''

    def __init__(self, connection: SQLitePublicationConnection, dictionary: bool) -> None:
        self.connection = connection
        self.dictionary = dictionary
        self.cursor = connection.connection.cursor()
        self.changed_rowcount = -1

    @property
    def rowcount(self) -> int:
        '''Expose MySQL changed rows, including zero for a matched no-op UPDATE.'''
        return self.changed_rowcount

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        '''Execute repository SQL locally after translating only MySQL syntax.'''
        statement = sql.replace(
            'TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP())',
            "datetime('now', '-' || %s || ' minutes')",
        )
        statement = statement.replace('UTC_TIMESTAMP()', 'CURRENT_TIMESTAMP')
        statement = statement.replace('FOR UPDATE', '').replace('<=>', 'IS').replace('%s', '?')
        is_update = statement.lstrip().lower().startswith('update rich_crpytonews')
        if is_update:
            before = {row['id']: tuple(row) for row in self.connection.connection.execute(
                'SELECT * FROM rich_crpytonews ORDER BY id',
            ).fetchall()}
        self.cursor.execute(statement, params)
        self.changed_rowcount = self.cursor.rowcount
        if is_update:
            after = {row['id']: tuple(row) for row in self.connection.connection.execute(
                'SELECT * FROM rich_crpytonews ORDER BY id',
            ).fetchall()}
            self.changed_rowcount = sum(before[key] != row for key, row in after.items())
            if self.cursor.rowcount and not self.changed_rowcount:
                self.connection.noop_updates.append(' '.join(sql.lower().split()))

    def fetchone(self) -> Any:
        '''Return the requested row representation without simulating its contents.'''
        row = self.cursor.fetchone()
        if row is None:
            return None
        return dict(row) if self.dictionary else tuple(row)

    def close(self) -> None:
        '''Close the real local cursor.'''
        self.cursor.close()


class SQLitePublicationConnection:
    '''Supply only the connector methods used by the actual publication repository.'''

    def __init__(self, path: str, *, fail_post_identity_commit: bool = False) -> None:
        self.connection = sqlite3.connect(path)
        self.fail_post_identity_commit = fail_post_identity_commit
        self.identity_commit_failures = 0
        self.noop_updates: list[str] = []
        self.connection.row_factory = sqlite3.Row

    def start_transaction(self) -> None:
        '''Start a real isolated SQLite transaction.'''
        self.connection.execute('BEGIN')

    def cursor(self, dictionary: bool = False) -> SQLitePublicationCursor:
        '''Create a cursor for the current isolated transaction.'''
        return SQLitePublicationCursor(self, dictionary)

    def commit(self) -> None:
        '''Commit locally, optionally failing the created-post pointer transaction.'''
        if self.fail_post_identity_commit:
            post_id = self.connection.execute(
                'SELECT wp_post_id FROM rich_crpytonews WHERE id = 11',
            ).fetchone()[0]
            if post_id is not None:
                self.identity_commit_failures += 1
                raise RuntimeError('synthetic post pointer commit failure')
        self.connection.commit()

    def rollback(self) -> None:
        '''Roll back the isolated fixture visibly on repository failure.'''
        self.connection.rollback()

    def close(self) -> None:
        '''Release the temporary local database connection.'''
        self.connection.close()


def create_restart_database(path: str) -> None:
    '''Create a minimal isolated application schema matching the repository's columns.'''
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
            CREATE TABLE rich_crpytonews (
                id INTEGER PRIMARY KEY, raw_article_id INTEGER, news_url TEXT,
                title TEXT, full_text TEXT, category TEXT, hashtags TEXT,
                seo_slug TEXT, seo_focus TEXT, seo_meta TEXT,
                published INTEGER, publish_status TEXT, publish_claim_token TEXT,
                publish_claimed_at TEXT, publish_attempt_count INTEGER,
                publish_last_error TEXT, publication_key TEXT, published_at TEXT,
                wp_post_id INTEGER, wp_post_url TEXT, wp_post_created_at TEXT,
                wp_media_id INTEGER, wp_media_metadata_json TEXT
            );
            CREATE TABLE cryptonewsapi (
                id INTEGER PRIMARY KEY, news_url TEXT, chosen_for_publish INTEGER,
                scheduled_for TEXT, is_breaking INTEGER
            );
        """)
        connection.execute(
            '''INSERT INTO rich_crpytonews
               (id, raw_article_id, news_url, title, full_text, category, hashtags,
                seo_slug, seo_focus, seo_meta, published, publish_status, publish_attempt_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (11, 22, 'https://source.example.invalid/news', 'Native $42: Éther’s outlook',
             '<p>A factual body.</p>', 'Markets', '', 'ether-outlook',
             'Éther outlook', 'Description: O\'Brien’s Éther is $42\u00a0today.', 0, 'fresh', 0),
        )
        connection.execute(
            '''INSERT INTO cryptonewsapi
               (id, news_url, chosen_for_publish, scheduled_for, is_breaking)
               VALUES (?, ?, ?, ?, ?)''',
            (22, 'https://source.example.invalid/news', 1, '2026-01-01 00:00:00', 0),
        )
        connection.commit()


def run_restart_attempt(app_path: str, wp_path: str, phase: str) -> dict[str, Any]:
    '''Run one actual service attempt; carry only serialized fixture state across processes.'''
    from publishing.service import PublishingService
    from repositories.publication import PublicationRepository

    http = FakeWordPress('timeout' if phase == 'first' else 'accepted')
    state_path = Path(wp_path)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        http.creates = state['creates']
        http.updates = state['updates']
        http.stored_meta = state['stored_meta']
    connections: list[SQLitePublicationConnection] = []

    def connect() -> SQLitePublicationConnection:
        connection = SQLitePublicationConnection(
            app_path, fail_post_identity_commit=phase == 'identity_commit_failure',
        )
        connections.append(connection)
        return connection

    repository = PublicationRepository(connect=connect)
    adapter = publisher.WordPressPublisher()
    image_preparer = MagicMock(return_value=(None, {}))
    persist_observed: list[int] = []
    original_post = http.post

    def observing_post(url: str, **kwargs: Any) -> FakeResponse:
        '''Inspect committed app state immediately before each synthetic metadata mutation.'''
        if not url.rstrip('/').endswith('/posts'):
            with closing(sqlite3.connect(app_path)) as connection:
                row = connection.execute(
                    'SELECT wp_post_id, publish_status FROM rich_crpytonews WHERE id = 11',
                ).fetchone()
            if row != (77, 'post_created'):
                raise AssertionError('Created post identity was not committed before metadata')
            persist_observed.append(row[0])
        return original_post(url, **kwargs)

    with (
        patch.object(publisher, 'session', http),
        patch.object(publisher, 'WP_API_URL', 'https://wordpress.example.invalid'),
        patch.object(http, 'post', side_effect=observing_post),
        patch.object(publisher, 'ensure_category', return_value=12),
        patch.object(publisher, 'ensure_term', return_value=13),
        patch.object(adapter, 'find_external_media', return_value=None),
        patch.object(publisher.mysql.connector, 'connect',
                     side_effect=AssertionError('WordPress SQL must not participate in post metadata or retry')),
    ):
        result = PublishingService(repository, adapter, image_preparer,
                                   fresh_start_after=None).publish_due(1)
    state_path.write_text(json.dumps({
        'creates': http.creates, 'updates': http.updates, 'stored_meta': http.stored_meta,
    }, ensure_ascii=False), encoding='utf-8')
    with closing(sqlite3.connect(app_path)) as connection:
        row = connection.execute(
            '''SELECT wp_post_id, wp_post_url, published, publish_status, publish_attempt_count
               FROM rich_crpytonews WHERE id = 11''',
        ).fetchone()
    return {
        'phase': phase, 'pid': os.getpid(), 'result': result,
        'state': list(row), 'creates': len(http.creates),
        'update_post_ids': [request['id'] for request in http.updates],
        'image_calls': image_preparer.call_count, 'persist_observed': persist_observed,
        'identity_noop_updates': sum(
            'set raw_article_id = coalesce' in sql
            for connection in connections for sql in connection.noop_updates
        ),
        'identity_commit_failures': sum(connection.identity_commit_failures for connection in connections),
    }


class DurableMetadataProcessRestartTests(unittest.TestCase):
    '''Reopen committed application and WordPress fake state in fresh interpreter processes.'''

    def test_sanitized_readback_keeps_application_seo_meta_and_post_payload_unchanged(self) -> None:
        '''A successful local service run must not save the comparison form into seo_meta.'''
        from publishing.service import PublishingService
        from repositories.publication import PublicationRepository

        description = '\tA  report\r\n with details. \x0b'
        sanitized_readback = 'A report with details.'
        with tempfile.TemporaryDirectory(prefix='yoast-whitespace-fixture-') as directory:
            path = str(Path(directory) / 'app.sqlite')
            create_restart_database(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('UPDATE rich_crpytonews SET seo_meta = ? WHERE id = 11', (description,))
                connection.commit()
            repository = PublicationRepository(connect=lambda: SQLitePublicationConnection(path))
            adapter = publisher.WordPressPublisher()
            http = FakeWordPress()
            original_get = http.get

            def sanitized_get(url: str, **kwargs: Any) -> FakeResponse:
                response = original_get(url, **kwargs)
                response.body['meta']['_yoast_wpseo_metadesc'] = sanitized_readback
                return response

            with (
                patch.object(publisher, 'session', http),
                patch.object(http, 'get', side_effect=sanitized_get),
                patch.object(publisher, 'WP_API_URL', 'https://wordpress.example.invalid'),
                patch.object(publisher, 'ensure_category', return_value=12),
                patch.object(publisher, 'ensure_term', return_value=13),
                patch.object(adapter, 'find_external_media', return_value=None),
                patch.object(publisher.mysql.connector, 'connect',
                             side_effect=AssertionError('WordPress SQL must not be called')),
            ):
                result = PublishingService(repository, adapter, MagicMock(return_value=(None, {})),
                                           fresh_start_after=None).publish_due(1)
            with closing(sqlite3.connect(path)) as connection:
                row = connection.execute(
                    'SELECT seo_meta, published, publish_status FROM rich_crpytonews WHERE id = 11',
                ).fetchone()
        self.assertEqual(result, {'attempted': 1, 'succeeded': 1, 'failed': 0})
        self.assertEqual(row, (description, 1, 'published'))
        self.assertEqual(http.updates[0]['json']['meta']['_yoast_wpseo_metadesc'], description)

    def restart_attempts(self, first_phase: str) -> tuple[dict[str, Any], dict[str, Any]]:
        '''Run actual production objects with independent process-local fixtures.'''
        root = Path(__file__).resolve().parents[2]
        bootstrap = r"""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

root = Path(sys.argv[1]).resolve()
project = root / 'GetNewsAPI'
sys.path[:0] = [str(project), str(root), str(Path(sys.executable).parent / 'Lib/site-packages')]
spec = importlib.util.spec_from_file_location('restart_environment', project / 'tests/environment_isolation.py')
if spec is None or spec.loader is None:
    raise RuntimeError('Cannot load the isolated fixture environment')
isolation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isolation)
preserved = {name: value for name, value in os.environ.items()
             if name.upper() in {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP'}}
os.environ.clear()
os.environ.update(preserved)
isolation.install_test_environment()
import dotenv
import mysql.connector
attempts = []
def blocked(*args, **kwargs):
    attempts.append('external connection')
    raise AssertionError('A restart regression attempted an unmocked external connection')
with ExitStack() as stack:
    stack.enter_context(patch.object(dotenv, 'load_dotenv', return_value=False))
    stack.enter_context(patch.object(socket, 'create_connection', side_effect=blocked))
    stack.enter_context(patch.object(socket, 'getaddrinfo', side_effect=blocked))
    stack.enter_context(patch.object(socket.socket, 'connect', side_effect=blocked))
    stack.enter_context(patch.object(socket.socket, 'connect_ex', side_effect=blocked))
    stack.enter_context(patch.object(mysql.connector, 'connect', side_effect=blocked))
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        from GetNewsAPI.tests.test_yoast_metadata_sync import run_restart_attempt
        result = run_restart_attempt(sys.argv[2], sys.argv[3], sys.argv[4])
    result['guard_attempts'] = attempts
print(json.dumps(result, ensure_ascii=False))
"""
        with tempfile.TemporaryDirectory(prefix='yoast-restart-fixture-') as directory:
            app_path = str(Path(directory) / 'app.sqlite')
            wp_path = str(Path(directory) / 'wordpress.json')
            create_restart_database(app_path)
            receipts: list[dict[str, Any]] = []
            for phase in (first_phase, 'second'):
                process = subprocess.run(
                    [sys.executable, '-I', '-S', '-B', '-c', bootstrap,
                     str(root), app_path, wp_path, phase],
                    check=False, capture_output=True, text=True, encoding='utf-8', timeout=30,
                )
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                receipts.append(json.loads(process.stdout))
        first, second = receipts
        self.assertNotEqual(first['pid'], second['pid'])
        return first, second

    def test_metadata_timeout_then_process_restart_reuses_committed_post_identity(self) -> None:
        first, second = self.restart_attempts('first')
        self.assertEqual(first['result'], {'attempted': 1, 'succeeded': 0, 'failed': 1})
        self.assertEqual(first['state'][0], 77)
        self.assertEqual(first['state'][1], 'https://wordpress.example.invalid/posts/77/')
        self.assertEqual(first['state'][2:4], [0, 'retryable'])
        self.assertEqual(first['persist_observed'], [77])
        self.assertEqual(second['result'], {'attempted': 1, 'succeeded': 1, 'failed': 0})
        self.assertEqual(second['state'][0], 77)
        self.assertEqual(second['state'][2:4], [1, 'published'])
        self.assertEqual(second['state'][4], 2)
        self.assertEqual(second['creates'], 1)
        self.assertEqual(second['update_post_ids'], [77, 77])
        self.assertEqual(second['image_calls'], 0)
        self.assertEqual(second['persist_observed'], [77])
        self.assertEqual(second['identity_noop_updates'], 1)
        self.assertFalse(first['guard_attempts'])
        self.assertFalse(second['guard_attempts'])


    def test_create_then_pointer_commit_failure_restart_requires_manual_reconciliation(self) -> None:
        first, second = self.restart_attempts('identity_commit_failure')
        self.assertEqual(first['result'], {'attempted': 1, 'succeeded': 0, 'failed': 1})
        self.assertEqual(first['identity_commit_failures'], 1)
        self.assertIsNone(first['state'][0])
        self.assertEqual(first['creates'], 1)
        self.assertEqual(first['update_post_ids'], [])
        self.assertEqual(second['result'], {'attempted': 1, 'succeeded': 0, 'failed': 1})
        self.assertIsNone(second['state'][0])
        self.assertEqual(second['state'][2:4], [0, 'manual_review'])
        self.assertEqual(second['creates'], 1)
        self.assertEqual(second['update_post_ids'], [])
        self.assertEqual(second['image_calls'], 0)
        self.assertFalse(first['guard_attempts'])
        self.assertFalse(second['guard_attempts'])

    def test_manual_review_row_does_not_starve_a_fresh_article_in_the_same_batch(self) -> None:
        from publishing.service import PublishingService
        from repositories.publication import PublicationRepository

        with tempfile.TemporaryDirectory(prefix='manual-review-batch-') as directory:
            path = str(Path(directory) / 'app.sqlite')
            create_restart_database(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("UPDATE rich_crpytonews SET publish_status='pending' WHERE id=11")
                connection.execute(
                    """INSERT INTO rich_crpytonews
                       (id, raw_article_id, news_url, title, full_text, category, hashtags,
                        seo_focus, seo_meta, published, publish_status, publish_attempt_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (12, 23, 'https://source.example.invalid/fresh', 'A fresh native headline',
                     '<p>A factual report.</p>', 'Markets', '', 'Fresh focus',
                     'A complete fresh description.', 0, 'fresh', 0),
                )
                connection.execute(
                    """INSERT INTO cryptonewsapi
                       (id, news_url, chosen_for_publish, scheduled_for, is_breaking)
                       VALUES (?, ?, ?, ?, ?)""",
                    (23, 'https://source.example.invalid/fresh', 1, '2026-02-01 00:00:00', 0),
                )
                connection.commit()
            repository = PublicationRepository(connect=lambda: SQLitePublicationConnection(path))
            adapter = publisher.WordPressPublisher()
            image = MagicMock(return_value=(None, {}))
            http = FakeWordPress()
            with (
                patch.object(publisher, 'session', http),
                patch.object(publisher, 'WP_API_URL', 'https://wordpress.example.invalid'),
                patch.object(publisher, 'ensure_category', return_value=12),
                patch.object(publisher, 'ensure_term', return_value=13),
                patch.object(adapter, 'find_external_media', return_value=None),
                patch.object(publisher.mysql.connector, 'connect',
                             side_effect=AssertionError('WordPress SQL must not be called')),
            ):
                result = PublishingService(repository, adapter, image, fresh_start_after=None).publish_due(2)
            with closing(sqlite3.connect(path)) as connection:
                rows = connection.execute(
                    'SELECT id, published, publish_status, wp_post_id FROM rich_crpytonews ORDER BY id',
                ).fetchall()
        self.assertEqual(result, {'attempted': 2, 'succeeded': 1, 'failed': 1})
        self.assertEqual(rows, [(11, 0, 'manual_review', None), (12, 1, 'published', 77)])
        self.assertEqual(len(http.creates), 1)
        self.assertEqual(image.call_count, 1)
