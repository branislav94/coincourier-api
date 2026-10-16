'''Offline coverage for coherent metadata without modifying article evidence.'''

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from GetNewsAPI.tests.environment_isolation import install_test_environment


install_test_environment()
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import gpt_processor
from publishing import PublicationArticle
from publishing.wordpress import seo


SOURCE_URL = 'https://issuer.example.invalid/dated-filing'
FIRST_SENTENCE = 'The filing reports Bitcoin holdings as of September 30, 2026.'


def article_document(**overrides: Any) -> dict[str, Any]:
    '''Keep actual required-field validation and metadata handoff active.'''
    document: dict[str, Any] = {
        'title': 'Company reports dated Bitcoin holdings',
        'full_text': (
            '<p>' + FIRST_SENTENCE + '</p><h2>Reporting context</h2><p>'
            + 'The filing distinguishes dated purchases from total balances. ' * 65
            + f'<a href="{SOURCE_URL}">Source filing</a>.</p>'
        ),
        'category': 'Institutional',
        'categories': ['Institutional'],
        'hashtags': 'bitcoin, holdings, disclosure',
        'sentiment': 0.0,
        'seo_focus': 'Bitcoin holdings',
        'seo_slug': 'company-bitcoin-holdings',
        'seo_meta': 'The company reports its Bitcoin holdings for the stated period.',
        'image_alt': 'A corporate treasury ledger',
        'schema_jsonld': '{"@type":"NewsArticle","description":"Existing schema"}',
    }
    document.update(overrides)
    return document


class ArticleMetaDescriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(
            gpt_processor, 'get_db_connection',
            side_effect=AssertionError('unexpected database access'),
        ))
        self.stack.enter_context(patch.object(
            gpt_processor, 'OpenAI', side_effect=AssertionError('unexpected provider access'),
        ))
        self.stack.enter_context(patch.object(
            gpt_processor, '_call_text_provider',
            side_effect=AssertionError('unexpected text-provider access'),
        ))

    def test_usable_generated_descriptions_outside_old_window_are_preserved(self) -> None:
        descriptions = (
            'The company reports dated Bitcoin holdings.',
            'A dated filing explains Bitcoin holdings and treasury purchases, separating '
            'the latest reported balance from the amount acquired during the reporting period '
            'and retaining the original as-of date.',
            'A dated filing explains treasury balances',
        )
        self.assertGreater(len(descriptions[1]), 170)
        self.assertLess(len(descriptions[1]), 240)
        for description in descriptions:
            with self.subTest(description=description):
                document = article_document(seo_meta=description)
                before = deepcopy(document)
                result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
                self.assertEqual(result['seo_meta'], description)
                self.assertEqual(result, before)
                self.assertEqual(document, before)

    def test_generated_unicode_and_nonbreaking_spaces_are_preserved(self) -> None:
        description = 'O\'Brien’s “Éther” filing reports $42\u00a0million in Zürich & 東京.'
        document = article_document(seo_meta=description)
        result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(result['seo_meta'], description)
        self.assertIn('\u00a0', result['seo_meta'])

    def test_html_metadata_is_cleaned_without_changing_body_or_unicode(self) -> None:
        description = '<strong>Éther’s</strong> filing reports $42&nbsp;million &amp; dated balances.'
        document = article_document(seo_meta=description)
        before = deepcopy(document)
        result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(result['seo_meta'], 'Éther’s  filing reports $42\u00a0million & dated balances.')
        self.assertEqual(result['full_text'], before['full_text'])
        self.assertEqual(document, before)

    def test_missing_and_blank_descriptions_use_an_intact_body_sentence(self) -> None:
        for description in (None, '', ' \t\n '):
            with self.subTest(description=description):
                document = article_document(seo_meta=description)
                result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
                self.assertEqual(result['seo_meta'], FIRST_SENTENCE)
                self.assertTrue(result['seo_meta'].endswith('.'))
                self.assertEqual(result['full_text'], document['full_text'])

    def test_incomplete_metadata_is_replaced_without_unrelated_keyword_suffix(self) -> None:
        incomplete = (
            'The filing reports Bitcoin holdings from its',
            'The company reports dated holdings and',
            'The company reports dated holdings…',
            'The company reports dated holdings...',
            'The company reports dated holdings,',
            'The company reports dated holdings:',
        )
        for description in incomplete:
            with self.subTest(description=description):
                document = article_document(seo_meta=description, seo_focus='quantum dominance')
                result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
                self.assertEqual(result['seo_meta'], FIRST_SENTENCE)
                self.assertNotIn('quantum dominance', result['seo_meta'])

    def test_useful_sentence_before_an_unfinished_tail_is_retained(self) -> None:
        description = FIRST_SENTENCE + ' The company announced further plans to'
        document = article_document(seo_meta=description)
        result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(result['seo_meta'], FIRST_SENTENCE)

    def test_excessive_description_keeps_a_complete_sentence_not_a_prefix(self) -> None:
        description = FIRST_SENTENCE + ' Additional dated context explains treasury decisions.' * 8
        self.assertGreater(len(description), 240)
        result = gpt_processor.apply_deterministic_seo_cleanup(
            article_document(seo_meta=description), {},
        )
        self.assertEqual(result['seo_meta'], FIRST_SENTENCE)

    def test_overlong_single_sentence_falls_back_to_intact_article_evidence(self) -> None:
        description = 'The company reports ' + 'additional dated treasury context ' * 12 + 'in its filing.'
        self.assertGreater(len(description), 240)
        result = gpt_processor.apply_deterministic_seo_cleanup(
            article_document(seo_meta=description), {},
        )
        self.assertEqual(result['seo_meta'], FIRST_SENTENCE)

    def test_unfinished_or_overlong_body_uses_the_factual_title_without_cropping(self) -> None:
        title = 'Company reports dated Bitcoin holdings'
        for body in (
            '<p>The company disclosed its holdings from its</p>',
            '<p>The company reports ' + 'additional dated treasury context ' * 12 + 'in its filing.</p>',
        ):
            with self.subTest(body=body):
                self.assertEqual(gpt_processor._fallback_meta(body, 'unrelated keyword', title), title)

    def test_fallback_returns_empty_when_no_coherent_evidence_is_available(self) -> None:
        for body, title in (('', ''), ('<p>from its</p>', 'The filing and')):
            with self.subTest(body=body, title=title):
                self.assertEqual(gpt_processor._fallback_meta(body, 'invented suffix', title), '')

    def test_initial_question_heading_does_not_replace_informative_paragraph(self) -> None:
        body = '<h2>What changed?</h2><p>' + FIRST_SENTENCE + '</p>'
        document = article_document(seo_meta='', full_text=body)
        result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(result['seo_meta'], FIRST_SENTENCE)
        self.assertEqual(result['full_text'], body)

    def test_initial_unpunctuated_heading_is_not_prepended_to_description(self) -> None:
        body = '<h2>Reporting context</h2><p>' + FIRST_SENTENCE + '</p>'
        document = article_document(seo_meta='', full_text=body)
        result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(result['seo_meta'], FIRST_SENTENCE)
        self.assertEqual(result['full_text'], body)

    def test_video_only_paragraph_is_skipped_without_changing_the_embed(self) -> None:
        video = '<p>https://www.youtube.com/watch?v=fixture123</p>'
        body = video + '<p>' + FIRST_SENTENCE + '</p>'
        document = article_document(seo_meta='', full_text=body)
        result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(result['seo_meta'], FIRST_SENTENCE)
        self.assertEqual(result['full_text'], body)
        self.assertEqual(result['full_text'].count(video), 1)

    def test_fallback_preserves_decimal_abbreviation_and_quoted_sentence_boundaries(self) -> None:
        sentences = (
            'Dr. Lee said U.S. holdings rose 3.5% in the reporting period.',
            'The filing says, “Holdings rose 3.5% in the stated period.”',
            '東京の開示資料は報告期間の保有残高を示しています。',
        )
        for sentence in sentences:
            with self.subTest(sentence=sentence):
                body = f'<p>{sentence} A second sentence adds context.</p>'
                self.assertEqual(gpt_processor._fallback_meta(body, 'unrelated'), sentence)

    def test_metadata_cleanup_preserves_html_video_schema_and_all_other_fields(self) -> None:
        document = article_document(seo_meta='The filing reports holdings from its')
        document['full_text'] += '<p>https://www.youtube.com/watch?v=fixture123</p><ul><li>Dated detail</li></ul>'
        before = deepcopy(document)
        raw = {'title': 'Original title', 'full_text': 'Original evidence', 'news_url': SOURCE_URL}
        raw_before = deepcopy(raw)
        result = gpt_processor.apply_deterministic_seo_cleanup(document, raw)
        self.assertEqual(result, {**before, 'seo_meta': FIRST_SENTENCE})
        self.assertEqual(document, before)
        self.assertEqual(raw, raw_before)

    def test_real_writer_validation_recovers_missing_metadata_without_provider_fallback(self) -> None:
        for mode in ('missing', 'empty', 'whitespace'):
            with self.subTest(mode=mode):
                document = article_document(seo_meta='' if mode != 'whitespace' else ' \t')
                if mode == 'missing':
                    del document['seo_meta']
                raw = {'title': 'Original report', 'full_text': 'Dated source evidence.', 'news_url': SOURCE_URL}
                before = deepcopy(raw)
                with (
                    patch.object(gpt_processor, 'PRIMARY_LLM_PROVIDER', 'grok'),
                    patch.object(gpt_processor, 'LLM_FALLBACK_PROVIDER', 'openai'),
                    patch.object(gpt_processor, '_call_text_provider', return_value=json.dumps(document)) as writer,
                ):
                    result, provider = gpt_processor.classify_and_rewrite(raw, '')
                self.assertEqual(provider, 'grok')
                self.assertEqual(result['seo_meta'], FIRST_SENTENCE)
                writer.assert_called_once()
                self.assertEqual(writer.call_args.args[0], 'grok')
                self.assertEqual(writer.call_args.kwargs['phase'], 'rewrite')
                self.assertEqual(result['full_text'], document['full_text'])
                self.assertEqual(raw, before)

    def test_missing_other_required_fields_remain_hard_failures(self) -> None:
        for key in ('category', 'hashtags', 'sentiment', 'seo_focus', 'seo_slug', 'image_alt'):
            with self.subTest(key=key):
                document = article_document(seo_meta='')
                del document[key]
                with self.assertRaises(gpt_processor.ArticleValidationError) as raised:
                    gpt_processor._validate_generated_article(document, {})
                self.assertIn(f'missing_{key}', raised.exception.failures)
                self.assertEqual(document['seo_meta'], FIRST_SENTENCE)

    def test_metadata_recovery_does_not_weaken_449_word_rejection(self) -> None:
        document = article_document(seo_meta='', full_text='<p>' + 'word ' * 449 + '</p>')
        with self.assertRaises(gpt_processor.ArticleValidationError) as raised:
            gpt_processor._validate_generated_article(document, {})
        self.assertEqual(raised.exception.failures, ['body_under_450_words'])
        self.assertEqual(document['seo_meta'], document['title'])

    def test_missing_or_invalid_body_remains_a_hard_failure_after_metadata_recovery(self) -> None:
        for body in (None, '', 42, {'unexpected': 'object'}, ['unexpected', 'list']):
            with self.subTest(body=body):
                document = article_document(seo_meta='', full_text=body)
                with self.assertRaises(gpt_processor.ArticleValidationError) as raised:
                    gpt_processor._validate_generated_article(document, {})
                self.assertTrue(
                    {'empty_full_text', 'missing_paragraph', 'body_under_450_words'}
                    .intersection(raised.exception.failures),
                )
                self.assertEqual(document['seo_meta'], document['title'])
        document = article_document(seo_meta='')
        del document['full_text']
        with self.assertRaises(gpt_processor.ArticleValidationError) as raised:
            gpt_processor._validate_generated_article(document, {})
        self.assertIn('missing_full_text', raised.exception.failures)
        self.assertIn('empty_full_text', raised.exception.failures)

    def test_metadata_only_differences_never_request_whole_article_repair(self) -> None:
        for description in ('A dated treasury filing.', '', 'The company reported holdings from its', FIRST_SENTENCE * 6):
            with self.subTest(description=description):
                document = article_document(seo_meta=description)
                before = deepcopy(document)
                with patch.object(gpt_processor, 'generate_json_with_provider') as repair:
                    result = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', document, 'grok')
                repair.assert_not_called()
                self.assertEqual(result['full_text'], before['full_text'])
                self.assertEqual(document, before)
                self.assertTrue(result['seo_meta'])

    def test_clean_document_is_returned_unchanged_without_repair(self) -> None:
        document = article_document()
        with patch.object(gpt_processor, 'generate_json_with_provider') as repair:
            result = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', document, 'grok')
        repair.assert_not_called()
        self.assertIs(result, document)

    def test_cleaned_metadata_reaches_rich_storage_and_yoast_mapping_intact(self) -> None:
        description = 'O\'Brien’s “Éther” filing reports $42\u00a0million & dated balances.'
        document = gpt_processor.apply_deterministic_seo_cleanup(article_document(seo_meta=description), {})
        raw = {'id': 17, 'news_url': SOURCE_URL, 'source_name': 'Issuer Filings', 'tickers': ['BTC']}
        for durable in (False, True):
            with self.subTest(durable=durable):
                connection = MagicMock()
                record = deepcopy(document)
                with (
                    patch.object(gpt_processor, 'PROCESS_DURABLE_CLAIMS_ENABLED', durable),
                    patch.object(gpt_processor, 'get_db_connection', return_value=connection),
                ):
                    gpt_processor.store_rich_news(record, raw)
                execute = connection.cursor.return_value.execute
                execute.assert_called_once()
                values = execute.call_args.args[1]
                self.assertEqual(values[-1], description)
                self.assertIn(document['full_text'], values)
                connection.commit.assert_called_once()
                mapped = PublicationArticle.from_mapping(record)
                self.assertEqual(mapped.seo_description, description)
                self.assertEqual(mapped.html_content, document['full_text'])
                metadata = seo.validate_yoast_metadata(
                    focus_keyword=mapped.seo_focus, description=mapped.seo_description, title=mapped.title,
                )
                self.assertEqual(metadata['_yoast_wpseo_metadesc'], description)
                self.assertEqual(set(metadata), {
                    '_yoast_wpseo_focuskw', '_yoast_wpseo_title', '_yoast_wpseo_metadesc',
                })


if __name__ == '__main__':
    unittest.main()
