'''Exercise editorial payloads and repair decisions without model-quality claims.'''

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from GetNewsAPI.tests.environment_isolation import install_test_environment


install_test_environment()
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import gpt_processor


SOURCE_URL = 'https://publisher.example.invalid/disclosure'
META_DESCRIPTION = (
    'Dated company filings explain changes in its Bitcoin holdings, separating purchases '
    'from balances and placing the figures in their stated reporting period.'
)


def article_document(*, heading: bool = True, external: bool = True) -> dict:
    '''A synthetic hard-valid body exposes routing rather than editorial accuracy.'''
    heading_html = '<h2>Reporting context</h2>' if heading else ''
    link = f'<a href="{SOURCE_URL}">Source filing</a>.' if external else 'Source filing.'
    body = heading_html + '<p>' + 'The filing reports dated holdings. ' * 95 + link + '</p>'
    return {
        'title': 'Company reports dated Bitcoin holdings',
        'full_text': body,
        'category': 'Institutional',
        'categories': ['Institutional'],
        'hashtags': 'bitcoin, holdings, disclosure',
        'sentiment': 0.0,
        'seo_focus': 'Bitcoin holdings',
        'seo_slug': 'company-bitcoin-holdings',
        'seo_meta': META_DESCRIPTION,
        'image_alt': 'A corporate treasury ledger',
        'schema_jsonld': '{"@type":"NewsArticle"}',
    }


class ArticleEditorialQualityTests(unittest.TestCase):
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

    def test_actual_writer_payload_retains_financial_market_and_update_evidence(self) -> None:
        cases = (
            (
                'Dated financial disclosure',
                'As of September 30, 2026, holdings were 1,000 BTC. September purchases were 200 BTC.',
                '',
            ),
            (
                'Superseded financial snapshot',
                'The September 30 statement reported 1,000 BTC, compared with 800 BTC on August 31.',
                '- October 14 research: a filing states holdings of 1,200 BTC as of October 13.',
            ),
            (
                'Purchase versus balance growth',
                'A purchase of 200 BTC increased holdings from 800 BTC to 1,000 BTC, a 25% rise.',
                '- The 200 BTC purchase equals 20% of the resulting 1,000 BTC balance.',
            ),
            (
                'Bitcoin price rises',
                'Bitcoin rose 2% to $65,000 over 24 hours; no market-share figures were supplied.',
                '- The cited venue reports the same price and comparison period.',
            ),
            (
                'Limited conflicting research',
                'The issuer proposed a launch in November; approval remains pending.',
                '- A secondary report calls it a launch, but the issuer still describes a proposal.',
            ),
            (
                'Material update rather than restatement',
                'An October 14 update changed the proposed launch to an approved October 20 launch.',
                '- The October 1 announcement described a proposal; the October 14 notice grants approval.',
            ),
        )
        for title, body, extra in cases:
            with self.subTest(title=title):
                original = {
                    'title': title, 'full_text': body, 'news_url': SOURCE_URL,
                    'source_name': 'Original Filings', 'publish_date': '2026-10-14 09:00:00',
                }
                before = deepcopy(original)
                expected = article_document()

                def generate(_model, **kwargs):
                    kwargs['validator'](expected)
                    return expected, 'grok'

                with patch.object(
                    gpt_processor, 'generate_json_with_primary_provider', side_effect=generate,
                ) as writer:
                    result, provider = gpt_processor.classify_and_rewrite(original, extra)
                messages = writer.call_args.kwargs['messages']
                source = json.loads(messages[1]['content'].split(
                    'ORIGINAL SOURCE EVIDENCE (provider text may be an excerpt):\n', 1,
                )[1])
                self.assertEqual(source['available_source_text'], body)
                self.assertEqual(source['source_title'], title)
                self.assertEqual(source['source_published_at'], original['publish_date'])
                self.assertIn(extra, messages[1]['content'])
                self.assertEqual(original, before)
                self.assertEqual(result, expected)
                self.assertEqual(provider, 'grok')
                writer.assert_called_once()

    def test_active_prompt_uses_neutral_examples_and_coordinated_editorial_instructions(self) -> None:
        # These assertions prove instructions sent, not that a future model follows them.
        def generate(_model, **kwargs):
            doc = article_document()
            kwargs['validator'](doc)
            return doc, 'grok'

        with patch.object(
            gpt_processor, 'generate_json_with_primary_provider', side_effect=generate,
        ) as writer:
            gpt_processor.classify_and_rewrite({'title': 'Quantum security research'}, '')
        system = writer.call_args.kwargs['messages'][0]['content']
        for required in (
            'as-of dates', 'purchases from cumulative holdings', 'correct denominator',
            'CoinCourier interpretation', 'uncertain outcomes', 'Announce',
            'Financial disclosures', 'ETF flows', 'Regulation', 'Material updates',
            'untrusted data', 'no fixed paragraph or sentence count',
            'use transitions only when their meaning fits', 'repetitive paragraph openings',
            '450-word hard minimum', 'VIDEO_URL', 'NewsArticle', 'categories', 'sentiment',
        ):
            self.assertIn(required, system)
        for removed in ('bitcoin dominance', '550 words', '600–800', '30–40%', '0.8–1.5%', '8-12 paragraphs'):
            self.assertNotIn(removed, system)

    def test_clean_hard_valid_short_body_avoids_repair_for_minor_soft_failures(self) -> None:
        doc = article_document()
        before = deepcopy(doc)
        status = gpt_processor.validate_readability_and_seo(doc)
        self.assertGreaterEqual(status['metrics']['words'], 450)
        self.assertLess(status['metrics']['words'], 550)
        self.assertTrue(status['checks']['len_ok'])
        self.assertTrue(status['checks']['h2_ok'])
        self.assertTrue(status['checks']['kw_in_slug'])
        self.assertFalse(status['checks']['transitions_ok'])
        self.assertFalse(status['checks']['kw_density_ok'])
        with patch.object(gpt_processor, 'generate_json_with_provider') as repair:
            result = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', doc, 'grok')
        repair.assert_not_called()
        self.assertEqual(result, before)
        self.assertEqual(doc, before)

    def test_short_useful_meta_and_repetitive_openers_do_not_add_a_repair_request(self) -> None:
        doc = {**article_document(), 'seo_meta': 'The company reports dated Bitcoin holdings.'}
        body = doc['full_text']
        self.assertTrue(gpt_processor.validate_readability_and_seo(doc)['checks']['meta_len_ok'])
        with patch.object(gpt_processor, 'generate_json_with_provider') as repair:
            result = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', doc, 'grok')
        repair.assert_not_called()
        self.assertEqual(result['full_text'], body)
        self.assertEqual(result['seo_meta'], doc['seo_meta'])

    def test_missing_source_link_without_an_available_source_does_not_trigger_repair(self) -> None:
        doc = article_document(external=False)
        with patch.object(gpt_processor, 'generate_json_with_provider') as repair:
            result = gpt_processor.repair_if_needed({}, '', doc, 'grok')
        repair.assert_not_called()
        self.assertEqual(result, doc)

    def test_missing_heading_uses_one_sticky_provider_repair_with_actual_allowed_sources(self) -> None:
        doc = article_document(heading=False)
        candidate = {**doc, 'full_text': '<h2>Reporting context</h2>' + doc['full_text']}
        original = {
            'news_url': SOURCE_URL,
            'full_text': '<a href="https://issuer.example.invalid/filing?utm_source=feed">Issuer filing</a>',
        }

        def generate(_provider, _model, **kwargs):
            kwargs['validator'](candidate)
            return candidate

        with patch.object(gpt_processor, 'generate_json_with_provider', side_effect=generate) as repair:
            result = gpt_processor.repair_if_needed(original, 'https://invented.example.invalid', doc, 'grok')
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args, ('grok', gpt_processor.REWRITE_MODEL))
        self.assertEqual(repair.call_args.kwargs['phase'], 'repair')
        messages = repair.call_args.kwargs['messages']
        self.assertEqual(json.loads(messages[1]['content']), doc)
        instructions = messages[2]['content']
        self.assertIn('Failed checks: h2_ok.', instructions)
        self.assertIn(SOURCE_URL, instructions)
        self.assertIn('https://issuer.example.invalid/filing', instructions)
        self.assertNotIn('utm_source', instructions)
        self.assertNotIn('invented.example.invalid', instructions)
        self.assertNotIn('30–40%', instructions)
        self.assertEqual(result, candidate)

    def test_hard_invalid_repair_falls_back_to_valid_original_without_another_call(self) -> None:
        doc = article_document(heading=False)
        original_body = doc['full_text']

        def generate(_provider, _model, **kwargs):
            invalid = {**doc, 'full_text': '<p>Unsupported short replacement.</p>'}
            kwargs['validator'](invalid)
            return invalid

        with patch.object(gpt_processor, 'generate_json_with_provider', side_effect=generate) as repair:
            result = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', doc, 'grok')
        repair.assert_called_once()
        self.assertEqual(result['full_text'], original_body)
        gpt_processor.validate_rewritten_article(result, {})

    def test_meaningful_slug_and_alt_text_survive_cleanup_without_forced_keyword_suffixes(self) -> None:
        doc = {**article_document(), 'seo_slug': 'company-treasury-disclosure'}
        result = gpt_processor.apply_deterministic_seo_cleanup(doc, {})
        self.assertEqual(result['seo_slug'], 'company-treasury-disclosure')
        self.assertEqual(result['image_alt'], 'A corporate treasury ledger')
        self.assertEqual(result['full_text'], doc['full_text'])

    def test_hard_word_minimum_remains_exactly_450(self) -> None:
        doc = article_document()
        gpt_processor.validate_rewritten_article({**doc, 'full_text': '<p>' + 'word ' * 450 + '</p>'}, {})
        with self.assertRaises(gpt_processor.ArticleValidationError) as raised:
            gpt_processor.validate_rewritten_article({**doc, 'full_text': '<p>' + 'word ' * 449 + '</p>'}, {})
        self.assertEqual(raised.exception.failures, ['body_under_450_words'])

    def test_existing_same_provider_expansion_remains_one_attempt_with_evidence_only_instructions(self) -> None:
        doc = article_document()
        short = {**doc, 'full_text': '<p>' + 'word ' * 449 + '</p>'}
        messages = [{'role': 'user', 'content': 'Existing original evidence.'}]
        before = deepcopy(messages)
        with patch.object(
            gpt_processor, '_call_text_provider',
            side_effect=[json.dumps(short), json.dumps(doc)],
        ) as provider:
            result = gpt_processor.generate_json_with_provider(
                'grok', 'existing-model', phase='rewrite', messages=messages,
                validator=lambda parsed: gpt_processor.validate_rewritten_article(parsed, {}),
            )
        self.assertEqual(result, doc)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual([call.args[0] for call in provider.call_args_list], ['grok', 'grok'])
        self.assertEqual([call.kwargs['phase'] for call in provider.call_args_list], ['rewrite', 'expansion'])
        expansion = provider.call_args.kwargs['messages']
        self.assertEqual(json.loads(expansion[-2]['content']), short)
        self.assertIn('hard minimum remains 450 words', expansion[-1]['content'])
        self.assertIn('do not manufacture material', expansion[-1]['content'])
        self.assertNotIn('550', expansion[-1]['content'])
        self.assertEqual(messages, before)

    def test_failed_expansion_keeps_hard_minimum_without_a_third_attempt(self) -> None:
        short = {**article_document(), 'full_text': '<p>' + 'word ' * 449 + '</p>'}
        with patch.object(gpt_processor, '_call_text_provider', return_value=json.dumps(short)) as provider:
            with self.assertRaises(gpt_processor.ArticleValidationError) as raised:
                gpt_processor.generate_json_with_provider(
                    'grok', 'existing-model', phase='rewrite', messages=[],
                    validator=lambda parsed: gpt_processor.validate_rewritten_article(parsed, {}),
                )
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(raised.exception.failures, ['body_under_450_words'])

    def test_other_hard_failures_do_not_create_an_expansion_stage(self) -> None:
        invalid = {**article_document(), 'seo_meta': ''}
        with patch.object(gpt_processor, '_call_text_provider', return_value=json.dumps(invalid)) as provider:
            with self.assertRaises(gpt_processor.ArticleValidationError):
                gpt_processor.generate_json_with_provider(
                    'grok', 'existing-model', phase='rewrite', messages=[],
                    validator=lambda parsed: gpt_processor.validate_rewritten_article(parsed, {}),
                )
        provider.assert_called_once()

    def test_grok_suffix_keeps_same_request_and_hard_minimum(self) -> None:
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{}'), finish_reason='stop')],
            model='existing-grok-model',
        )
        original_messages = [{'role': 'system', 'content': 'Existing writer prompt.'}]
        with patch.object(gpt_processor, 'OpenAI', return_value=client):
            result = gpt_processor.call_grok_text(phase='rewrite', messages=original_messages)
        self.assertEqual(result, '{}')
        client.chat.completions.create.assert_called_once()
        sent = client.chat.completions.create.call_args.kwargs
        self.assertEqual(sent['model'], gpt_processor.GROK_TEXT_MODEL)
        self.assertIn('hard minimum is 450 words', sent['messages'][-1]['content'])
        self.assertNotIn('550', sent['messages'][-1]['content'])
        self.assertEqual(original_messages, [{'role': 'system', 'content': 'Existing writer prompt.'}])


if __name__ == '__main__':
    unittest.main()
