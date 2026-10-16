'''Offline regression coverage for the evidence supplied to existing AI stages.'''

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from GetNewsAPI.tests.environment_isolation import install_test_environment


install_test_environment()
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import gpt_processor


EVIDENCE_MARKER = 'ORIGINAL SOURCE EVIDENCE (provider text may be an excerpt):\n'


def valid_document() -> dict:
    '''Keep real hard validation active without requiring a provider response.'''
    return {
        'title': 'Company reports dated Bitcoin holdings',
        'full_text': '<p>' + 'The company reported dated holdings. ' * 95 + '</p>',
        'category': 'Institutional',
        'categories': ['Institutional'],
        'hashtags': 'bitcoin, company, disclosure',
        'sentiment': 0.0,
        'seo_focus': 'Bitcoin holdings',
        'seo_slug': 'company-bitcoin-holdings',
        'seo_meta': 'The company reported its Bitcoin holdings for the stated period.',
        'image_alt': 'Illustration of company Bitcoin holdings',
    }


class ArticleSourceEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(gpt_processor, 'USE_WEB_SEARCH', True))
        self.stack.enter_context(patch.object(
            gpt_processor, 'get_db_connection',
            side_effect=AssertionError('unexpected database access'),
        ))
        self.stack.enter_context(patch.object(
            gpt_processor, 'OpenAI', side_effect=AssertionError('unexpected provider access'),
        ))
        self.research = self.stack.enter_context(patch.object(
            gpt_processor, 'call_openai_enrichment', return_value='- Existing research context.',
        ))
        self.document = valid_document()

        def generate(_model, **kwargs):
            kwargs['validator'](self.document)
            return self.document, 'grok'

        self.writer = self.stack.enter_context(patch.object(
            gpt_processor, 'generate_json_with_primary_provider', side_effect=generate,
        ))

    @staticmethod
    def evidence(prompt: str) -> dict:
        return json.loads(prompt.split(EVIDENCE_MARKER, 1)[1])

    def writer_prompt(self) -> str:
        return self.writer.call_args.kwargs['messages'][1]['content']

    def test_database_full_text_reaches_both_existing_stages_without_mutation(self) -> None:
        article = {
            'id': 7, 'title': 'Dated disclosure', 'source_name': 'Original Publisher',
            'news_url': 'https://publisher.example.invalid/report',
            'publish_date': datetime(2026, 10, 14, 9, 30),
            'full_text': 'The company held 1,200 BTC as of October 14, 2026.',
        }
        before = deepcopy(article)
        extra = gpt_processor.enrich_with_search(article)
        result = gpt_processor.classify_and_rewrite(article, extra)

        research_evidence = self.evidence(self.research.call_args.args[0])
        writer_evidence = self.evidence(self.writer_prompt())
        self.assertEqual(research_evidence, writer_evidence)
        self.assertEqual(writer_evidence['available_source_text'], article['full_text'])
        self.assertEqual(writer_evidence['source_title'], article['title'])
        self.assertEqual(writer_evidence['source_publisher'], article['source_name'])
        self.assertEqual(writer_evidence['source_url'], article['news_url'])
        self.assertEqual(writer_evidence['source_published_at'], '2026-10-14T09:30:00+00:00')
        self.assertTrue(writer_evidence['source_text_may_be_excerpt'])
        self.assertFalse(writer_evidence['source_text_truncated'])
        self.assertEqual(article, before)
        self.assertIs(result[0], self.document)
        self.assertEqual(result[1], 'grok')
        self.research.assert_called_once()
        self.writer.assert_called_once()

    def test_legacy_text_preferred_and_blank_or_invalid_values_fall_back(self) -> None:
        for legacy, expected in (
            (' Legacy provider evidence. ', 'Legacy provider evidence.'),
            (' \n\t ', 'Database evidence.'),
            ('', 'Database evidence.'),
            (None, 'Database evidence.'),
            ({'not': 'source text'}, 'Database evidence.'),
        ):
            with self.subTest(legacy=legacy):
                article = {'text': legacy, 'full_text': ' Database evidence. '}
                before = deepcopy(article)
                gpt_processor.enrich_with_search(article)
                gpt_processor.classify_and_rewrite(article, '')
                self.assertEqual(
                    self.evidence(self.research.call_args.args[0])['available_source_text'], expected,
                )
                self.assertEqual(self.evidence(self.writer_prompt())['available_source_text'], expected)
                self.assertEqual(article, before)

    def test_missing_source_fields_remain_null_without_invented_dates(self) -> None:
        before = datetime.now(timezone.utc)
        gpt_processor.enrich_with_search({})
        gpt_processor.classify_and_rewrite({}, '')
        after = datetime.now(timezone.utc)
        prompts = (
            (self.research.call_args.args[0], 'Research request UTC: '),
            (self.writer_prompt(), 'GENERATION UTC: '),
        )
        for prompt, clock_label in prompts:
            evidence = self.evidence(prompt)
            for field in ('source_title', 'source_publisher', 'source_url', 'source_published_at'):
                self.assertIsNone(evidence[field])
            self.assertEqual(evidence['available_source_text'], '')
            self.assertFalse(evidence['source_text_truncated'])
            generated = datetime.fromisoformat(prompt.split(clock_label, 1)[1].split('\n', 1)[0])
            self.assertEqual(generated.utcoffset(), timedelta(0))
            self.assertLessEqual(before, generated)
            self.assertLessEqual(generated, after)

    def test_database_datetime_and_caller_dates_keep_their_actual_meaning(self) -> None:
        cases = (
            ({'publish_date': datetime(2026, 9, 30, 23, 0)}, '2026-09-30T23:00:00+00:00'),
            ({'date': datetime(2026, 10, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))},
             '2026-10-01T01:00:00+02:00'),
            ({'publish_date': ' ', 'date': ' Wed, 14 Oct 2026 09:00:00 +0000 '},
             'Wed, 14 Oct 2026 09:00:00 +0000'),
            ({'publish_date': '2026-10-14 09:00:00', 'date': 'older caller date'},
             '2026-10-14 09:00:00'),
            ({'publish_date': 123456789}, None),
        )
        for article, expected in cases:
            with self.subTest(article=article):
                gpt_processor.classify_and_rewrite(article, '')
                self.assertEqual(self.evidence(self.writer_prompt())['source_published_at'], expected)

    def test_source_and_research_context_are_bounded_independently(self) -> None:
        article = {'title': 'Bounded excerpt', 'full_text': 'x' * 6100}
        gpt_processor.enrich_with_search(article)
        gpt_processor.classify_and_rewrite(article, 'r' * 6100)
        research = self.evidence(self.research.call_args.args[0])
        writer = self.evidence(self.writer_prompt())
        self.assertEqual(research['available_source_text'], 'x' * 1200)
        self.assertEqual(writer['available_source_text'], 'x' * 6000)
        self.assertTrue(research['source_text_truncated'])
        self.assertTrue(writer['source_text_truncated'])
        self.assertIn('r' * 6000, self.writer_prompt())
        self.assertNotIn('r' * 6001, self.writer_prompt())
        self.assertNotIn('x' * 6001, self.writer_prompt())

    def test_incomplete_excerpt_is_not_presented_as_complete_source_content(self) -> None:
        article = {'text': 'The company announced plans, subject to approval…'}
        gpt_processor.enrich_with_search(article)
        gpt_processor.classify_and_rewrite(article, '')
        for prompt in (self.research.call_args.args[0], self.writer_prompt()):
            evidence = self.evidence(prompt)
            self.assertTrue(evidence['source_text_may_be_excerpt'])
            self.assertFalse(evidence['source_text_truncated'])
            self.assertEqual(evidence['available_source_text'], article['text'])

    def test_dated_financial_values_remain_separate_from_newer_research(self) -> None:
        article = {
            'title': 'Company reports September Bitcoin balance',
            'source_name': 'Company Filings', 'publish_date': '2026-10-01 09:00:00',
            'full_text': ('As of September 30, 2026, the company held 1,000 BTC. '
                          'Its September purchases were 200 BTC, compared with 800 BTC held in August.'),
        }
        extra = ('- An October 14 filing reported 1,200 BTC held as of October 13. '
                 'The newer filing does not establish purchases of 1,200 BTC.')
        gpt_processor.classify_and_rewrite(article, extra)
        prompt = self.writer_prompt()
        evidence = self.evidence(prompt)
        self.assertEqual(evidence['available_source_text'], article['full_text'])
        self.assertEqual(evidence['source_published_at'], article['publish_date'])
        self.assertIn(extra, prompt.split(EVIDENCE_MARKER, 1)[0])
        self.assertNotIn(extra, evidence['available_source_text'])
        for value in ('1,000 BTC', '200 BTC', '800 BTC', 'September 30, 2026'):
            self.assertIn(value, evidence['available_source_text'])

    def test_original_links_are_allowed_without_trusting_research_prose_urls(self) -> None:
        official = 'https://official.example.invalid/statement'
        article = {
            'news_url': 'https://publisher.example.invalid/story',
            'full_text': '<p>Source <a href="https://issuer.example.invalid/report?utm_source=news">filing</a>.</p>',
        }
        extra = f'- Official research citation: {official}\n\nSources:\n- https://invented.example.invalid/item'
        gpt_processor.classify_and_rewrite(article, extra)
        prompt = self.writer_prompt()
        allowed = prompt.split('ALLOWED_SOURCES:\n', 1)[1].split('\n\n', 1)[0]
        self.assertIn(article['news_url'], allowed)
        self.assertIn('https://issuer.example.invalid/report', allowed)
        self.assertNotIn('utm_source', allowed)
        self.assertNotIn(official, allowed)
        self.assertNotIn('invented.example.invalid', allowed)
        self.assertIn(official, prompt)

    def test_existing_json_video_and_writer_request_contracts_are_preserved(self) -> None:
        video = 'https://www.youtube.com/watch?v=example'
        result, provider = gpt_processor.classify_and_rewrite({'text': 'Source evidence.'}, '', video)
        kwargs = self.writer.call_args.kwargs
        self.assertEqual(self.writer.call_args.args, (gpt_processor.REWRITE_MODEL,))
        self.assertEqual(kwargs['response_format'], {'type': 'json_object'})
        self.assertEqual(kwargs['phase'], 'rewrite')
        self.assertEqual(kwargs['timeout_read'], 300)
        self.assertIsNone(kwargs['max_completion_tokens'])
        self.assertIn(f'VIDEO_URL (empty if none): {video}', self.writer_prompt())
        self.assertIs(result, self.document)
        self.assertEqual(provider, 'grok')
        self.assertEqual(result['categories'], ['Institutional'])
        self.assertIsInstance(result['sentiment'], float)
        self.assertIsInstance(result['hashtags'], str)
        self.assertGreaterEqual(gpt_processor.word_count(result['full_text']), 450)
        with self.assertRaises(gpt_processor.ArticleValidationError):
            kwargs['validator']({**self.document, 'full_text': '<p>Too short.</p>'})


if __name__ == '__main__':
    unittest.main()
