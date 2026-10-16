'''Check punctuation and existing provider boundaries using synthetic local output.'''

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


SOURCE_URL = 'https://publisher.example.invalid/dated-filing'
PUNCTUATED_SENTENCE = (
    'On October 14, 2026, the company held 1,234 BTC, worth $82,345,678.90; '
    'its reported balance rose 2.5%. '
    'The filing said, “These are dated figures,” and distinguished purchases from holdings.'
)


def article_document(*, heading: bool = True) -> dict:
    '''Synthetic hard-valid content tests byte preservation, not editorial quality.'''
    heading_html = '<h2>Dated reporting context</h2>' if heading else ''
    body = (
        heading_html + '<p>' + PUNCTUATED_SENTENCE + '</p><p>'
        + 'The dated filing describes holdings in the stated reporting period. ' * 45
        + f'<a href="{SOURCE_URL}">Source filing</a>.</p>'
    )
    return {
        'title': 'Company reports dated Bitcoin holdings',
        'full_text': body,
        'category': 'Institutional',
        'categories': ['Institutional'],
        'hashtags': 'bitcoin, holdings, disclosure',
        'sentiment': 0.0,
        'seo_focus': 'Bitcoin holdings',
        'seo_slug': 'company-bitcoin-holdings',
        'seo_meta': 'A dated company filing distinguishes new purchases from total Bitcoin holdings.',
        'image_alt': 'A corporate treasury ledger',
        'schema_jsonld': '{"@type":"NewsArticle"}',
    }


class ArticleQualityV11Tests(unittest.TestCase):
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

    def test_grok_sdk_string_and_multipart_content_preserve_punctuation(self) -> None:
        document = article_document()
        serialized = json.dumps(document, ensure_ascii=False)
        split = serialized.index('1,234') + 2
        contents = (
            serialized,
            [
                {'type': 'text', 'text': serialized[:split]},
                SimpleNamespace(text=serialized[split:]),
            ],
        )
        for content in contents:
            with self.subTest(multipart=isinstance(content, list)):
                client = MagicMock()
                client.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=content), finish_reason='stop',
                    )],
                    model='mocked-grok-model',
                )
                with patch.object(gpt_processor, 'OpenAI', return_value=client):
                    response = gpt_processor.call_grok_text(
                        phase='rewrite', messages=[], response_format={'type': 'json_object'},
                    )
                self.assertEqual(response, serialized)
                self.assertEqual(json.loads(response)['full_text'], document['full_text'])
                client.chat.completions.create.assert_called_once()

    def test_actual_writer_json_parsing_preserves_punctuation_for_both_providers(self) -> None:
        raw = {'title': 'Original filing', 'full_text': PUNCTUATED_SENTENCE, 'news_url': SOURCE_URL}
        for provider in ('grok', 'openai'):
            with self.subTest(provider=provider):
                document = article_document()
                with (
                    patch.object(gpt_processor, 'PRIMARY_LLM_PROVIDER', provider),
                    patch.object(gpt_processor, 'LLM_FALLBACK_PROVIDER', 'disabled'),
                    patch.object(
                        gpt_processor, '_call_text_provider',
                        return_value=json.dumps(document, ensure_ascii=False),
                    ) as writer,
                ):
                    result, selected = gpt_processor.classify_and_rewrite(raw, '')
                self.assertEqual(result, document)
                self.assertEqual(result['full_text'], document['full_text'])
                self.assertEqual(selected, provider)
                gpt_processor.validate_rewritten_article(result, raw)
                writer.assert_called_once()
                self.assertEqual(writer.call_args.args[0], provider)
                self.assertEqual(writer.call_args.kwargs['phase'], 'rewrite')

    def test_same_provider_expansion_preserves_draft_and_returned_punctuation(self) -> None:
        for provider in ('grok', 'openai'):
            with self.subTest(provider=provider):
                expanded = article_document()
                short = {**expanded, 'full_text': f'<p>{PUNCTUATED_SENTENCE}</p>'}
                messages = [{'role': 'user', 'content': PUNCTUATED_SENTENCE}]
                before = deepcopy(messages)
                with patch.object(
                    gpt_processor, '_call_text_provider',
                    side_effect=[json.dumps(short, ensure_ascii=False), json.dumps(expanded, ensure_ascii=False)],
                ) as writer:
                    result = gpt_processor.generate_json_with_provider(
                        provider, gpt_processor.REWRITE_MODEL, phase='rewrite', messages=messages,
                        validator=lambda doc: gpt_processor._validate_generated_article(doc, {}),
                    )
                self.assertEqual(result['full_text'], expanded['full_text'])
                self.assertEqual(writer.call_count, 2)
                self.assertEqual([call.args[0] for call in writer.call_args_list], [provider, provider])
                self.assertEqual(
                    [call.kwargs['phase'] for call in writer.call_args_list], ['rewrite', 'expansion'],
                )
                draft_message = writer.call_args.kwargs['messages'][-2]
                self.assertEqual(json.loads(draft_message['content']), short)
                instruction = writer.call_args.kwargs['messages'][-1]['content']
                self.assertIn('do not restate conclusions', instruction)
                self.assertIn('missing research/disclosures just to add words', instruction)
                self.assertIn('Preserve natural English punctuation', instruction)
                self.assertIn('hard minimum remains 450 words', instruction)
                self.assertEqual(messages, before)

    def test_structural_repair_preserves_input_json_and_fresh_response_punctuation(self) -> None:
        for provider in ('grok', 'openai'):
            with self.subTest(provider=provider):
                draft = article_document(heading=False)
                before = deepcopy(draft)
                repaired = {**draft, 'full_text': '<h2>Dated reporting context</h2>' + draft['full_text']}
                with patch.object(
                    gpt_processor, '_call_text_provider',
                    return_value=json.dumps(repaired, ensure_ascii=False),
                ) as writer:
                    result = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', draft, provider)
                self.assertEqual(result['full_text'], repaired['full_text'])
                self.assertEqual(draft, before)
                writer.assert_called_once()
                self.assertEqual(writer.call_args.args[0], provider)
                self.assertEqual(writer.call_args.kwargs['phase'], 'repair')
                sent = writer.call_args.kwargs['messages']
                self.assertEqual(json.loads(sent[1]['content']), before)
                self.assertIn('Do not multiply caveats, missing-disclosure explanations or conclusions', sent[2]['content'])
                self.assertIn('commas in prose, numerical separators and dates', sent[2]['content'])
                gpt_processor.validate_rewritten_article(result, {})

    def test_writer_requests_punctuation_distinct_context_and_concise_search_focus(self) -> None:
        # Payload coverage does not establish how a future model will write.
        document = article_document()
        with patch.object(
            gpt_processor, '_call_text_provider', return_value=json.dumps(document),
        ) as writer:
            result, _provider = gpt_processor.classify_and_rewrite(
                {'title': 'Original filing', 'full_text': PUNCTUATED_SENTENCE}, '',
            )
        system = writer.call_args.kwargs['messages'][0]['content']
        for instruction in (
            'publication-ready journalistic English with natural commas',
            '1,234.5 and October 14, 2026',
            'State each material limitation briefly where it changes interpretation',
            'preserve necessary uncertainty',
            'do not repeat caveats or list missing research/disclosures to fill space',
            'Give each paragraph a distinct supported fact or explanation',
            'explain each implication once',
            'concise search phrase, usually 2-5 words',
            'Avoid headline-length clauses and unnecessary dates',
            'retain essential names and qualifiers',
            'No fixed density or keyword stuffing',
            '450-word hard minimum',
        ):
            self.assertIn(instruction, system)
        self.assertEqual(result, document)
        writer.assert_called_once()

    def test_focus_length_guidance_does_not_truncate_or_stuff_output_fields(self) -> None:
        for focus in ('dated treasury filing', 'company Bitcoin holdings for the stated financial reporting period'):
            with self.subTest(focus=focus):
                document = {**article_document(), 'seo_focus': focus}
                before = deepcopy(document)
                with patch.object(
                    gpt_processor, '_call_text_provider', return_value=json.dumps(document),
                ) as writer:
                    result, provider = gpt_processor.classify_and_rewrite(
                        {'title': 'Original filing', 'news_url': SOURCE_URL}, '',
                    )
                    final = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', result, provider)
                writer.assert_called_once()
                self.assertEqual(final, before)
                self.assertEqual(final['seo_focus'], focus)

    def test_metadata_cleanup_and_no_repair_path_preserve_punctuated_body(self) -> None:
        document = {**article_document(), 'seo_meta': ''}
        before = deepcopy(document)
        cleaned = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(cleaned['full_text'], before['full_text'])
        self.assertEqual(document, before)
        with patch.object(gpt_processor, '_call_text_provider') as writer:
            result = gpt_processor.repair_if_needed({'news_url': SOURCE_URL}, '', document, 'grok')
        writer.assert_not_called()
        self.assertEqual(result['full_text'], before['full_text'])
        gpt_processor.validate_rewritten_article(result, {})

    def test_sql_bound_body_preserves_punctuation_in_legacy_and_durable_storage(self) -> None:
        document = article_document()
        raw = {'id': 123, 'news_url': SOURCE_URL, 'source_name': 'Issuer Filings', 'tickers': ['BTC']}
        for durable in (False, True):
            with self.subTest(durable=durable):
                record = deepcopy(document)
                connection = MagicMock()
                with (
                    patch.object(gpt_processor, 'PROCESS_DURABLE_CLAIMS_ENABLED', durable),
                    patch.object(gpt_processor, 'get_db_connection', return_value=connection),
                ):
                    gpt_processor.validate_rewritten_article(record, raw)
                    gpt_processor.store_rich_news(record, raw)
                execute = connection.cursor.return_value.execute
                execute.assert_called_once()
                bound_values = execute.call_args.args[1]
                self.assertEqual(bound_values[3 if durable else 2], document['full_text'])
                self.assertEqual(record['full_text'], document['full_text'])
                connection.commit.assert_called_once()

    def test_failed_grok_expansion_keeps_existing_openai_fallback_order(self) -> None:
        document = article_document()
        short = {**document, 'full_text': f'<p>{PUNCTUATED_SENTENCE}</p>'}
        raw = {'title': 'Original filing', 'full_text': PUNCTUATED_SENTENCE, 'news_url': SOURCE_URL}
        with (
            patch.object(gpt_processor, 'PRIMARY_LLM_PROVIDER', 'grok'),
            patch.object(gpt_processor, 'LLM_FALLBACK_PROVIDER', 'openai'),
            patch.object(
                gpt_processor, '_call_text_provider',
                side_effect=[json.dumps(short), json.dumps(short), json.dumps(document)],
            ) as writer,
        ):
            result, provider = gpt_processor.classify_and_rewrite(raw, '')
        self.assertEqual(provider, 'openai')
        self.assertEqual(result['full_text'], document['full_text'])
        self.assertEqual(writer.call_count, 3)
        self.assertEqual([call.args[0] for call in writer.call_args_list], ['grok', 'grok', 'openai'])
        self.assertEqual(
            [call.kwargs['phase'] for call in writer.call_args_list], ['rewrite', 'expansion', 'rewrite'],
        )

    def test_zero_comma_body_is_not_mechanically_punctuated(self) -> None:
        document = article_document()
        document['full_text'] = document['full_text'].replace(',', '')
        before = deepcopy(document)
        self.assertNotIn(',', before['full_text'])
        result = gpt_processor.apply_deterministic_seo_cleanup(document, {})
        self.assertEqual(result['full_text'], before['full_text'])
        self.assertEqual(document, before)
        gpt_processor.validate_rewritten_article(result, {})


if __name__ == '__main__':
    unittest.main()
