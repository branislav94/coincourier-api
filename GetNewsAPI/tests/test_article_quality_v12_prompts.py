'''Verify bounded v1.2 request instructions, not future retrieval or factual accuracy.'''

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
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


CUTOFF = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
SOURCE_URL = 'https://publisher.example.invalid/dated-report'
EVIDENCE_MARKER = 'ORIGINAL SOURCE EVIDENCE (provider text may be an excerpt):\n'
AUM_EVIDENCE = (
    'August 25, 2026: BNB Chain ecosystem bStocks AUM was $500 million. '
    'October 15, 2026 at 07:15 UTC: Binance bStocks AUM was $1 billion; this is a different platform scope. '
    'October 15, 2026 at 09:00 UTC: BNB Chain ecosystem bStocks AUM was $600 million, the same scope as August. '
    'October 16, 2026 at 09:00 UTC: a later issuer announcement reports $700 million, after the research cutoff. '
    'A separate source claims $900 million without a compatible definition, platform or as-of date.'
)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return CUTOFF.astimezone(tz) if tz is not None else CUTOFF.replace(tzinfo=None)


def generated_document() -> dict:
    '''Synthetic hard-valid output checks request plumbing without a model call.'''
    return {
        'title': 'Issuer reports a dated balance',
        'full_text': '<h2>Reporting context</h2><p>' + 'The filing reports dated holdings. ' * 95
                     + f'<a href="{SOURCE_URL}">Source filing</a>.</p>',
        'category': 'Institutional',
        'categories': ['Institutional'],
        'hashtags': 'issuer, holdings, disclosure',
        'sentiment': 0.0,
        'seo_focus': 'Issuer balance',
        'seo_slug': 'issuer-dated-balance',
        'seo_meta': 'The issuer reports its balance for the stated period, with dated figures and their reporting scope.',
        'image_alt': 'A corporate treasury ledger',
        'schema_jsonld': '{"@type":"NewsArticle"}',
    }


def completed_research(text: str):
    return SimpleNamespace(
        status='completed', error=None, incomplete_details=None,
        output=[
            SimpleNamespace(type='web_search_call', status='completed'),
            SimpleNamespace(
                type='message', role='assistant', status='completed',
                content=[SimpleNamespace(type='output_text', text=text, annotations=[])],
            ),
        ],
    )


class ArticleQualityV12PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(gpt_processor, 'datetime', FixedDatetime))
        self.stack.enter_context(patch.object(gpt_processor, 'USE_WEB_SEARCH', True))
        self.stack.enter_context(patch.object(
            gpt_processor, 'get_db_connection', side_effect=AssertionError('unexpected database access'),
        ))
        self.stack.enter_context(patch.object(
            gpt_processor, 'OpenAI', side_effect=AssertionError('unexpected provider access'),
        ))

    def test_freshness_fixture_reaches_one_existing_research_request_with_unchanged_settings(self) -> None:
        original = {
            'id': 501, 'title': 'Dated bStocks developments', 'full_text': AUM_EVIDENCE,
            'news_url': SOURCE_URL, 'publish_date': '2026-10-14 12:00:00',
        }
        before = deepcopy(original)
        client = MagicMock()
        client.__enter__.return_value = client
        client.responses.create.return_value = completed_research('- Dated context, retaining metric scopes.')
        with (
            patch.object(gpt_processor, 'OpenAI', return_value=client) as constructor,
            patch.object(gpt_processor, 'OPENAI_API_KEY', 'synthetic-test-key'),
            patch.object(gpt_processor, 'ENRICHMENT_MODEL', 'synthetic-research-model'),
            patch.object(gpt_processor, 'ENRICHMENT_REASONING_EFFORT', 'low'),
            patch.object(gpt_processor, 'ENRICHMENT_SEARCH_CONTEXT_SIZE', 'low'),
            patch.object(gpt_processor, 'ENRICHMENT_MAX_OUTPUT_TOKENS', 2048),
        ):
            result = gpt_processor.enrich_with_search(original)
        constructor.assert_called_once_with(api_key='synthetic-test-key', timeout=45, max_retries=0)
        client.responses.create.assert_called_once()
        request = client.responses.create.call_args.kwargs
        prompt = request['input']
        self.assertEqual({key: value for key, value in request.items() if key != 'input'}, {
            'model': 'synthetic-research-model', 'reasoning': {'effort': 'low'},
            'tools': [{'type': 'web_search', 'search_context_size': 'low'}],
            'tool_choice': 'required', 'store': False, 'max_output_tokens': 2048,
        })
        self.assertIn('Research request UTC: 2026-10-16T08:00:00+00:00', prompt)
        evidence = json.loads(prompt.split(EVIDENCE_MARKER, 1)[1])
        self.assertEqual(evidence['available_source_text'], AUM_EVIDENCE)
        self.assertFalse(evidence['source_text_truncated'])
        self.assertEqual(original, before)
        self.assertEqual(result, '- Dated context, retaining metric scopes.')

    def test_research_instructions_prioritize_material_updates_and_compatible_scope_before_cutoff(self) -> None:
        with patch.object(gpt_processor, 'call_openai_enrichment', return_value='- Context.') as research:
            gpt_processor.enrich_with_search({'title': 'bStocks update', 'full_text': AUM_EVIDENCE})
        research.assert_called_once()
        prompt = research.call_args.args[0]
        # These assertions establish instructions sent; no search is performed.
        lowered = prompt.lower()
        for concept in ('newer', 'primary', 'cutoff', 'scope', 'definition', 'platform', 'geograph'):
            self.assertIn(concept, lowered)
        self.assertRegex(lowered, r'compar.{0,100}compatib')
        self.assertRegex(lowered, r'(missing|absen).{0,100}(proof|does not|not prove)')
        self.assertIn('at most three concise bullet points', prompt)
        self.assertIn('provider text may be an excerpt', prompt)
        self.assertIn(AUM_EVIDENCE, json.loads(prompt.split(EVIDENCE_MARKER, 1)[1])['available_source_text'])

    def test_disabled_research_remains_no_request(self) -> None:
        with (
            patch.object(gpt_processor, 'USE_WEB_SEARCH', False),
            patch.object(gpt_processor, 'call_openai_enrichment') as research,
        ):
            self.assertEqual(gpt_processor.enrich_with_search({'full_text': AUM_EVIDENCE}), '')
        research.assert_not_called()

    def _writer_messages(self, original: dict, research: str):
        expected = generated_document()
        before = deepcopy(original)
        with (
            patch.object(gpt_processor, 'PRIMARY_LLM_PROVIDER', 'grok'),
            patch.object(gpt_processor, 'LLM_FALLBACK_PROVIDER', 'disabled'),
            patch.object(gpt_processor, '_call_text_provider', return_value=json.dumps(expected)) as writer,
        ):
            result, provider = gpt_processor.classify_and_rewrite(original, research)
        writer.assert_called_once()
        self.assertEqual(writer.call_args.args, ('grok', gpt_processor.REWRITE_MODEL))
        self.assertEqual(writer.call_args.kwargs['phase'], 'rewrite')
        self.assertEqual(writer.call_args.kwargs['response_format'], {'type': 'json_object'})
        self.assertEqual(writer.call_args.kwargs['timeout_read'], 300)
        self.assertIsNone(writer.call_args.kwargs['max_completion_tokens'])
        self.assertEqual(result, expected)
        self.assertEqual(provider, 'grok')
        self.assertEqual(original, before)
        return writer.call_args.kwargs['messages']

    def test_legal_actor_roles_are_conditional_and_source_roles_are_preserved_in_payload(self) -> None:
        original = {
            'id': 502, 'title': 'Sterlingov conviction appeal', 'news_url': SOURCE_URL,
            'full_text': (
                'Roman Sterlingov appealed his Bitcoin Fog conviction. The D.C. Circuit upheld the conviction. '
                'Chainalysis supplied Reactor analytics; it was an evidence provider, not the appellant.'
            ),
        }
        research = '- The ruling concerns Sterlingov, the defendant and appellant, not an appeal filed by Chainalysis.'
        messages = self._writer_messages(original, research)
        system, payload = messages[0]['content'], messages[1]['content']
        conditional = system.split('WHEN APPLICABLE', 1)[1].split('FORMATTING', 1)[0].lower()
        for role in ('appellant', 'defendant', 'regulator', 'court'):
            self.assertIn(role, conditional)
        self.assertRegex(conditional, r'evidence providers?.{0,120}(parties|litigation|appellant)')
        self.assertRegex(conditional, r'(filed|appeal).{0,100}(ruling|ruled)')
        self.assertEqual(json.loads(payload.split(EVIDENCE_MARKER, 1)[1])['available_source_text'], original['full_text'])
        self.assertIn(research, payload)

    def test_exact_comparison_dates_and_baselines_are_sent_without_automatic_rewriting(self) -> None:
        original = {
            'id': 503, 'title': 'Dated Bitcoin and dollar snapshots', 'news_url': SOURCE_URL,
            'full_text': (
                'Bitcoin closed at 85,771 on October 5, 2026, 81,604 on October 14, '
                'and 82,156 on October 15. The dollar index was 101.45 on September 30 and 102.06 on October 15.'
            ),
        }
        research = (
            '- October 5 to October 14 is nine elapsed days; October 5 to October 15 is ten. '
            'A separate report cites 7.3% BTC and 3.2% DXY monthly changes without compatible start dates.'
        )
        messages = self._writer_messages(original, research)
        system, payload = messages[0]['content'].lower(), messages[1]['content']
        for concept in ('start', 'end dates', 'elapsed days', 'baseline', 'windows'):
            self.assertIn(concept, system)
        self.assertRegex(system, r'(exact|supplied).{0,100}dates')
        self.assertRegex(system, r'(incompatible|compatible).{0,100}(observations|windows|periods)')
        self.assertEqual(json.loads(payload.split(EVIDENCE_MARKER, 1)[1])['available_source_text'], original['full_text'])
        self.assertIn(research, payload)
        self.assertIn('450-word hard minimum', system)
        self.assertIn('usually 2-5 words', system)


if __name__ == '__main__':
    unittest.main()
