'''Offline integration coverage; synthetic model output proves no editorial outcome.'''

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from GetNewsAPI.tests.environment_isolation import install_test_environment

install_test_environment()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gpt_processor as gp
from GetNewsAPI.tests.test_article_quality_v11 import article_document, SOURCE_URL
from GetNewsAPI.tests.test_openai_enrichment import response_with_text


BENZINGA = ('https://www.benzinga.com/crypto/cryptocurrency/26/10/62351460/'
            'is-a-stronger-dollar-bullish-for-bitcoin')
PRIMARY = 'https://issuer.example.invalid/announcement?id=12&page=2'
PROSE = 'https://unverified.example.invalid/not-an-annotation'


def citation(url=PRIMARY, **metadata):
    return SimpleNamespace(type='url_citation', url=url, **metadata)


class ArticleQualityV12IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('OpenAI', 'get_db_connection'):
            self.stack.enter_context(patch.object(gp, name, side_effect=AssertionError('unexpected external call')))
        self.stack.enter_context(patch.object(gp.requests, 'post', side_effect=AssertionError('unexpected HTTP call')))
        self.stack.enter_context(patch.dict(os.environ, {gp.ENABLE_ENV: 'false'}))

    def processing(self):
        for name in ('_run_duplicate_shadow_fail_open', '_run_semantic_shadow_fail_open', 'mark_processed'):
            self.stack.enter_context(patch.object(gp, name))
        self.stack.enter_context(patch.object(gp, '_maybe_video_url', return_value=''))

    def enable_capture(self, recorder):
        self.stack.enter_context(patch.dict(os.environ, {gp.ENABLE_ENV: 'true'}))
        return self.stack.enter_context(patch.object(gp, '_configured_quality_capture', return_value=recorder))

    def test_primary_extracted_and_scoped_sources_share_normalized_deduplication(self):
        raw = {'news_url': BENZINGA + '?utm_source=snapi', 'text': (
            f'<a href="{BENZINGA}?utm_medium=feed">Same</a>'
            '<a href="https://issuer.example.invalid/report?id=12&amp;utm_source=x">Report</a>'
        )}
        before = deepcopy(raw)
        with gp.research_citation_scope():
            gp._enrichment_response_text(response_with_text('A dated fact.', [citation()]))
            self.assertEqual(gp._allowed_article_sources(raw), [
                BENZINGA, 'https://issuer.example.invalid/report?id=12', PRIMARY,
            ])
        self.assertEqual(raw, before)
        self.assertEqual(gp.current_research_citations(), ())

    def test_research_string_and_annotation_metadata_are_bounded_without_prose_permissions(self):
        annotations = [citation(PRIMARY, title='Issuer announcement', start_index=0, end_index=5),
                       citation(PRIMARY + '&utm_source=x'), citation('https://regulator.example.invalid/order'),
                       citation('https://exchange.example.invalid/notice'), citation('https://fourth.example.invalid/')]
        with gp.research_citation_scope():
            result = gp._enrichment_response_text(response_with_text(PROSE + ' x' * 6000, annotations))
            self.assertIs(type(result), str)
            self.assertLessEqual(len(result), 6000)
            retained = gp.current_research_citations()
            self.assertEqual(len(retained), 3)
            self.assertEqual(retained[0].title, 'Issuer announcement')
            self.assertEqual((retained[0].start_index, retained[0].end_index), (0, 5))
            self.assertNotIn(PROSE, gp._allowed_article_sources({'news_url': SOURCE_URL}))
            self.assertEqual(gp._allowed_article_sources({'news_url': SOURCE_URL})[0], SOURCE_URL)

    def test_unusable_later_output_never_leaves_partial_or_previous_citations(self):
        with gp.research_citation_scope():
            gp._enrichment_response_text(response_with_text('Good fact', [citation()]))
            bad = response_with_text('Another fact', [citation('https://other.example.invalid/')])
            bad.output.append(SimpleNamespace(type='message', role='assistant', status='incomplete'))
            with self.assertRaises(gp.EnrichmentRequestError):
                gp._enrichment_response_text(bad)
            self.assertEqual(gp.current_research_citations(), ())

    def test_bad_optional_annotation_spans_are_omitted_and_titles_bounded(self):
        with gp.research_citation_scope():
            gp._enrichment_response_text(response_with_text('Fact', [
                citation(title='Title ' * 100, start_index=0, end_index=500),
            ]))
            retained = gp.current_research_citations()[0]
            self.assertLessEqual(len(retained.title), 200)
            self.assertIsNone(retained.start_index)
            self.assertIsNone(retained.end_index)

    def test_writer_and_repair_receive_same_real_annotation_sources_in_one_processing_scope(self):
        self.processing()
        raw = {'id': 20, 'title': 'Source report', 'news_url': SOURCE_URL}
        draft = article_document(heading=False)
        final = article_document()
        with (
            patch.object(gp, 'enrich_with_search', side_effect=lambda _: gp._enrichment_response_text(
                response_with_text('Official evidence. ' + PROSE, [citation()]))),
            patch.object(gp, '_call_text_provider', side_effect=[json.dumps(draft), json.dumps(final)]) as provider,
            patch.object(gp, 'store_rich_news') as store,
        ):
            self.assertTrue(gp.process_one(raw))
        self.assertEqual(provider.call_count, 2)
        writer, repair = provider.call_args_list
        for call in (writer, repair):
            payload = '\n'.join(message['content'] for message in call.kwargs['messages'])
            allowed = payload.split('ALLOWED_SOURCES:\n', 1)[1].split('\n\n', 1)[0]
            self.assertIn(SOURCE_URL, allowed)
            self.assertIn(PRIMARY, allowed)
            self.assertNotIn(PROSE, allowed)
        self.assertEqual([c.kwargs['phase'] for c in provider.call_args_list], ['rewrite', 'repair'])
        store.assert_called_once()
        self.assertEqual(gp.current_research_citations(), ())

    def test_failed_process_resets_annotations_before_next_article(self):
        self.processing()
        raw = {'id': 21, 'title': 'Source report', 'news_url': SOURCE_URL}
        with (
            patch.object(gp, 'enrich_with_search', side_effect=lambda _: gp._enrichment_response_text(
                response_with_text('Evidence', [citation()]))),
            patch.object(gp, 'classify_and_rewrite', side_effect=ValueError('synthetic rejection')),
            patch.object(gp, 'store_rich_news') as store,
        ):
            self.assertFalse(gp.process_one(raw))
        self.assertEqual(gp.current_research_citations(), ())
        self.assertEqual(gp._allowed_article_sources(raw), [SOURCE_URL])
        store.assert_not_called()

    def test_final_sql_cleans_only_approved_href_preserving_raw_identity_and_punctuation(self):
        self.processing()
        raw = {'id': 22, 'title': 'Dated report', 'news_url': BENZINGA + '?utm_source=snapi'}
        for durable in (False, True):
            with self.subTest(durable=durable):
                doc = article_document()
                doc['full_text'] = doc['full_text'].replace(SOURCE_URL, raw['news_url'])
                doc['full_text'] += '<p><a href="/internal?utm_source=keep">Related</a></p>'
                before = deepcopy(raw)
                connection = MagicMock()
                with (
                    patch.object(gp, 'PROCESS_DURABLE_CLAIMS_ENABLED', durable),
                    patch.object(gp, 'enrich_with_search', return_value=''),
                    patch.object(gp, 'classify_and_rewrite', return_value=(doc, 'grok')),
                    patch.object(gp, 'repair_if_needed', return_value=doc),
                    patch.object(gp, 'get_db_connection', return_value=connection),
                ):
                    self.assertTrue(gp.process_one(raw, mark_complete=False))
                binds = connection.cursor.return_value.execute.call_args.args[1]
                self.assertIn(raw['news_url'], binds)
                body = binds[3 if durable else 2]
                self.assertEqual(body, doc['full_text'].replace(BENZINGA + '?utm_source=snapi', BENZINGA))
                self.assertIn('/internal?utm_source=keep', body)
                self.assertEqual(raw, before)
                connection.commit.assert_called_once()

    def test_final_cleanup_preserves_signed_destination_and_unapproved_links(self):
        signed = PRIMARY + '&utm_source=feed&sig=a%2Bb'
        doc = article_document()
        doc['full_text'] += f'<a href="{signed.replace("&", "&amp;")}">Signed</a>'
        doc['full_text'] += f'<a href="{PROSE}?utm_source=keep">Other</a>'
        before = deepcopy(doc)
        self.assertIs(gp._clean_final_article_links(doc, {'news_url': signed}), doc)
        self.assertEqual(doc, before)

    def test_disabled_capture_never_configures_or_writes(self):
        with patch.object(gp, '_configured_quality_capture') as configure:
            gp._capture_quality_stage('rewrite.response', article_context={'id': 1}, raw_text='private')
        configure.assert_not_called()

    def test_capture_setup_and_write_failures_do_not_change_provider_calls_or_output(self):
        document = article_document()
        for setup_fails in (False, True):
            with self.subTest(setup_fails=setup_fails):
                recorder = MagicMock()
                recorder.record_stage.side_effect = OSError('private capture unavailable')
                configure = self.enable_capture(recorder)
                configure.side_effect = OSError('bad capture path') if setup_fails else None
                with patch.object(gp, '_call_text_provider', return_value=json.dumps(document)) as provider:
                    result = gp.generate_json_with_provider('grok', gp.REWRITE_MODEL, phase='rewrite',
                        article_context={'id': 1}, validator=lambda d: gp.validate_rewritten_article(d, {}))
                self.assertEqual(result, document)
                provider.assert_called_once()

    def test_grok_response_is_captured_before_length_rejection_with_exact_attempt(self):
        recorder = MagicMock()
        self.enable_capture(recorder)
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"full_text":"<p>On October 14, 2026, 1,234 units.</p>"}'),
            finish_reason='length')])
        with patch.object(gp, 'OpenAI', return_value=client), patch.object(gp, 'GROK_API_KEY', 'fake-only'):
            with self.assertRaisesRegex(RuntimeError, 'truncated'):
                gp.call_grok_text(phase='rewrite', article_context={'id': 1}, messages=[])
        self.assertEqual([c.args[0] for c in recorder.record_stage.call_args_list], ['rewrite.request', 'rewrite.response'])
        event = recorder.record_stage.call_args.kwargs
        self.assertEqual(event['attempt'], 1)
        self.assertEqual(event['finish_reason'], 'length')
        self.assertIn('1,234', event['raw_text'])
        self.assertEqual(event['model'], 'grok_text')
        self.assertNotIn('request_messages', recorder.record_stage.call_args_list[0].kwargs)
        client.chat.completions.create.assert_called_once()

    def test_untyped_multipart_suppresses_capture_without_changing_generation(self):
        recorder = MagicMock()
        self.enable_capture(recorder)
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=[{'type': 'reasoning', 'text': 'hidden'}, {'type': 'text', 'text': 'final'}]),
            finish_reason='stop')])
        with patch.object(gp, 'OpenAI', return_value=client), patch.object(gp, 'GROK_API_KEY', 'fake-only'):
            # Existing generation concatenation is deliberately untouched.
            self.assertEqual(gp.call_grok_text(phase='rewrite', article_context={'id': 1}, messages=[]), 'hiddenfinal')
        self.assertIsNone(recorder.record_stage.call_args.kwargs['raw_text'])
        self.assertTrue(recorder.record_stage.call_args.kwargs['suppress_evidence'])

    def test_invalid_json_is_captured_before_parsing_rejection(self):
        recorder = MagicMock()
        self.enable_capture(recorder)
        with patch.object(gp, '_call_text_provider', return_value='not JSON, at all') as provider:
            with self.assertRaisesRegex(RuntimeError, 'invalid JSON'):
                gp.generate_json_with_provider('grok', 'model', phase='rewrite', article_context={'id': 1})
        recorder.record_stage.assert_called_once()
        self.assertEqual(recorder.record_stage.call_args.args[0], 'rewrite.pre_parse')
        provider.assert_called_once()

    def test_existing_expansion_capture_keeps_trigger_and_provider_count(self):
        recorder = MagicMock()
        self.enable_capture(recorder)
        doc = article_document()
        short = {**doc, 'full_text': '<p>Only one short, supported fact.</p>'}
        with patch.object(gp, '_call_text_provider', side_effect=[json.dumps(short), json.dumps(doc)]) as provider:
            result = gp.generate_json_with_provider('grok', gp.REWRITE_MODEL, phase='rewrite', article_context={'id': 1},
                validator=lambda d: gp._validate_generated_article(d, {}))
        self.assertEqual(result, doc)
        self.assertEqual([c.kwargs['phase'] for c in provider.call_args_list], ['rewrite', 'expansion'])
        events = recorder.record_stage.call_args_list
        self.assertEqual([c.args[0] for c in events], ['rewrite.pre_parse', 'rewrite.parsed',
            'rewrite.validation_failed', 'expansion.pre_parse', 'expansion.parsed'])
        self.assertEqual(events[2].kwargs['validation_failures'], ['body_under_450_words'])

    def test_selected_checks_and_final_capture_match_stored_body(self):
        self.processing()
        recorder = MagicMock()
        self.enable_capture(recorder)
        raw = {'id': 1, 'news_url': BENZINGA + '?utm_source=snapi'}
        doc = article_document(heading=False)
        doc['full_text'] = doc['full_text'].replace(SOURCE_URL, raw['news_url'])
        repaired = {**doc, 'full_text': '<h2>Dated reporting context</h2>' + doc['full_text']}
        with (
            patch.object(gp, 'enrich_with_search', return_value=''),
            patch.object(gp, 'classify_and_rewrite', return_value=(doc, 'grok')),
            patch.object(gp, '_call_text_provider', return_value=json.dumps(repaired)) as provider,
            patch.object(gp, 'store_rich_news') as store,
        ):
            self.assertTrue(gp.process_one(raw))
        events = {c.args[0]: c.kwargs for c in recorder.record_stage.call_args_list}
        self.assertEqual(events['repair.checks']['checks']['selected_repair_triggers'], ['h2_ok'])
        self.assertIn('?utm_source=snapi', events['repair.selected_before_cleanup']['document']['full_text'])
        self.assertEqual(events['article.before_storage']['document'], store.call_args.args[0])
        self.assertEqual(events['repair.post_cleanup']['document']['full_text'], repaired['full_text'])
        self.assertEqual(events['article.post_cleanup']['document'], store.call_args.args[0])
        self.assertEqual(events['article.before_storage']['validation_status'], 'passed')
        self.assertNotIn('?utm_source=snapi', store.call_args.args[0]['full_text'])
        provider.assert_called_once()

    def test_capture_adapter_uses_only_controlled_model_roles(self):
        recorder = MagicMock()
        self.enable_capture(recorder)
        for provider, model, expected in (
            ('grok', gp.GROK_TEXT_MODEL, 'grok_text'),
            ('openai', gp.REWRITE_MODEL, 'openai_text'),
            ('grok', 'synthetic-private-model-secret', 'unknown'),
            ('synthetic-private-provider-secret', gp.REWRITE_MODEL, 'unknown'),
        ):
            gp._capture_quality_stage('rewrite.response', article_context={'id': 1},
                provider=provider, model=model)
            self.assertEqual(recorder.record_stage.call_args.kwargs['model'], expected)

    def test_capture_configuration_never_reads_or_passes_credential_values(self):
        gp._configured_quality_capture.cache_clear()
        self.addCleanup(gp._configured_quality_capture.cache_clear)
        with patch.object(gp, 'configure_capture', return_value=None) as configure:
            gp._configured_quality_capture('private-directory', 'authorization-path',
                'verified-dev-host', 'verified_dev_db', 'https://dev.example.invalid')
        self.assertEqual(configure.call_args.kwargs, {
            'database_host': 'verified-dev-host', 'database_name': 'verified_dev_db',
            'wordpress_api_url': 'https://dev.example.invalid',
        })

    def test_openai_malformed_responses_do_not_dump_private_body_to_logs(self):
        response = MagicMock(status_code=200, text='private-body-fake-secret')
        response.json.side_effect = ValueError('invalid')
        with patch.object(gp.requests, 'post', return_value=response) as post, patch.object(gp, 'MAX_RETRIES', 1):
            with self.assertLogs(level='ERROR') as logged, self.assertRaises(ValueError):
                gp.call_openai('model', messages=[])
        self.assertNotIn(response.text, '\n'.join(logged.output))
        post.assert_called_once()

    def test_openai_nonretryable_error_logs_metadata_without_body(self):
        response = MagicMock(status_code=401, text='private-body-fake-secret')
        response.raise_for_status.side_effect = gp.requests.HTTPError('synthetic unauthorized')
        with patch.object(gp.requests, 'post', return_value=response) as post:
            with self.assertLogs(level='ERROR') as logged, self.assertRaises(gp.requests.HTTPError):
                gp.call_openai('model', messages=[])
        self.assertNotIn(response.text, '\n'.join(logged.output))
        post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
