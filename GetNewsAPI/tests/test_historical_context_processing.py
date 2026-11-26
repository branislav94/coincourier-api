'''Exercise historical context through article processing and WordPress creation.'''

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

from GetNewsAPI.tests.environment_isolation import install_test_environment


install_test_environment()
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import gpt_processor as gp
import historical_context
from duplicate_detection.enforcement import DuplicateDecision
from duplicate_detection.policy import AssessmentType, assess_relationship
from embeddings.chunking import prepare_document
from publishing.models import PublicationArticle, PublicationContext
from publishing.wordpress import publisher
from repositories.raw_news import RawNewsRepository, SourceArticleSnapshot
from semantic_retrieval.models import SemanticCandidate, SemanticRetrievalSettings, SemanticRetrievalStatus
from semantic_retrieval.service import SemanticRetrievalService
from semantic_retrieval.shadow import SemanticShadowRunResult
from vector_store.models import SourcePassage, SourceType, VectorDocumentRecord, VectorMatch
from vector_store.repository import VectorRepository
from vector_store.semantic_assessments import SemanticAssessmentStatus


class FixedDateTime(datetime):
    '''Freeze the generation timestamp in the actual provider request.'''

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        timestamp = cls(2026, 11, 26, 11, tzinfo=UTC)
        return timestamp.replace(tzinfo=None) if tz is None else timestamp.astimezone(tz)


SOURCE_URL = 'https://current.example.invalid/meridian-resumes'
ARCHIVE_URL = 'https://archive.example.invalid/meridian-paused'
REFERRAL_URL = 'https://exchange.example.invalid/join?ref=courier'
QUERY_TIME = FixedDateTime(2026, 11, 26, 10)
REPORT_TIME = datetime(2026, 11, 25, 8)
EMBEDDING_VERSION = 'offline-fixture-v1'
LEAD = '<p>Meridian Vault resumed withdrawals after operators completed a review.</p>'
PAUSE_PASSAGE = 'Meridian Vault paused withdrawals on November 24, 2026 after a pricing oracle malfunctioned.'
CORRECTED_PASSAGE = 'Correction: Meridian Vault never paused withdrawals on November 24, 2026.'
RESEARCH = '- Coinbase described its own withdrawal review process in a separate statement.'


def current_article() -> dict[str, Any]:
    '''Return the ingest row for a synthetic current source report.'''
    return {
        'id': 101,
        'title': 'Meridian Vault resumes withdrawals',
        'full_text': 'Meridian Vault resumed withdrawals on November 26, 2026 after its operators completed a review.',
        'news_url': SOURCE_URL,
        'source_name': 'Current Report',
        'publish_date': QUERY_TIME,
        'insertDate': QUERY_TIME + timedelta(minutes=1),
        'tickers': ['BTC'],
    }


def writer_document(raw: dict[str, Any], *, lead: str = LEAD) -> dict[str, Any]:
    '''Return a long synthetic repair output with independent citations and referral text.'''
    source = raw['news_url']
    return {
        'title': raw['title'],
        'full_text': (
            lead + '<h2>What the operators said</h2><p>'
            + 'The operators described their latest review in a written statement. ' * 45
            + f'Coinbase gave a separate statement. <a href="{source}">Current report</a>.</p>'
        ),
        'category': 'Institutional',
        'categories': ['Institutional'],
        'hashtags': 'vault, withdrawals',
        'sentiment': 0.0,
        'seo_focus': 'vault withdrawals',
        'seo_slug': 'meridian-vault-withdrawals',
        'seo_meta': 'Meridian Vault reopened withdrawals after operators completed a review of the service.',
        'image_alt': 'Vault withdrawal status screen',
        'schema_jsonld': '{"@type":"NewsArticle"}',
    }


def archive_body(matched_text: str) -> str:
    '''Put the matched passage after two chunks of unrelated archival copy.'''
    first = 'archive entry ' * 270
    second = 'publication record ' * 270
    return f'{first.rstrip()}\n\n{second.rstrip()}\n\n{matched_text}'


def indexed_passage(
    *, document_id: int = 501, source_id: int = 81,
    title: str = 'Meridian Vault paused withdrawals', text: str = PAUSE_PASSAGE,
    url: str | None = ARCHIVE_URL, published: datetime = REPORT_TIME,
    source_type: SourceType = SourceType.SOURCE_ARTICLE, distance: float = 0.02,
    raw: dict[str, Any] | None = None,
) -> tuple[SemanticCandidate, SourcePassage]:
    '''Build a matching candidate and a passage row for the vector repository boundary.'''
    current = current_article() if raw is None else raw
    source_key = f'{source_type.value}:{source_id}'
    prepared = prepare_document(title, archive_body(text))
    chunk = prepared.chunks[-1]
    document = VectorDocumentRecord(
        id=document_id, document_key=source_key, source_type=source_type, source_article_id=source_id,
        rich_article_id=None if source_type is SourceType.SOURCE_ARTICLE else source_id,
        source_url=url, title=title, published_at=published,
        content_hash=prepared.content_hash, content_version=prepared.content_version,
    )
    candidate = SemanticCandidate(
        query_document_id=700, query_source_article_id=current['id'],
        candidate_document_id=document_id, candidate_source_article_id=source_id,
        candidate_document_key=source_key, candidate_source_url=url, candidate_title=title,
        native_distance=distance, embedding_version=EMBEDDING_VERSION, best_query_chunk_index=0,
        best_candidate_chunk_index=chunk.index, best_candidate_chunk_id=document_id + 1,
        best_candidate_chunk_hash=chunk.sha256, matched_query_chunk_count=1, published_at=published,
        publication_delta_hours=(current['publish_date'] - published).total_seconds() / 3600,
        source_type=source_type,
    )
    passage = SourcePassage(
        document=document, chunk_index=chunk.index, chunk_text=chunk.text, embedding_version=EMBEDDING_VERSION,
        chunk_id=document_id + 1, chunk_hash=chunk.sha256,
    )
    return candidate, passage


def semantic_result(
    *candidates: SemanticCandidate,
    raw: dict[str, Any] | None = None,
    status: SemanticAssessmentStatus = SemanticAssessmentStatus.RETRIEVED,
    retrieval_status: SemanticRetrievalStatus = SemanticRetrievalStatus.RETRIEVED,
    reason: str | None = None,
) -> SemanticShadowRunResult:
    '''Represent the single existing semantic run for this source article.'''
    current = current_article() if raw is None else raw
    return SemanticShadowRunResult(
        status=status, reason=reason, query_source_article_id=current['id'],
        query_document_id=700, query_published_at=current['publish_date'],
        embedding_version=EMBEDDING_VERSION, semantic_version='semantic-shadow-v1',
        retrieval_status=retrieval_status, candidate_count=len(candidates), candidates=candidates,
    )


class HistoricalContextProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        '''Install provider and storage doubles without bypassing rewrite or repair.'''
        self.repository = Mock(spec_set=VectorRepository)
        self.repository.get_source_passages.return_value = {}
        self.vector = self.enterContext(patch.object(historical_context, 'VectorRepository', return_value=self.repository))
        self.raw_repository = Mock(spec_set=RawNewsRepository)
        self.source_snapshots: dict[int, SourceArticleSnapshot | None] = {}
        self.raw_repository.get_source_article_snapshot.side_effect = self.source_snapshots.get
        self.enterContext(patch.object(gp, 'RawNewsRepository', return_value=self.raw_repository))
        self.semantic = self.enterContext(patch.object(gp, '_run_semantic_shadow_fail_open'))
        self.writer = self.enterContext(patch.object(gp, '_call_text_provider'))
        self.research = self.enterContext(patch.object(gp, 'enrich_with_search', return_value=RESEARCH))
        self.store = self.enterContext(patch.object(gp, 'store_rich_news'))
        self.mark_processed = self.enterContext(patch.object(gp, 'mark_processed'))
        self.enterContext(patch.object(gp, 'datetime', FixedDateTime))
        self.enterContext(patch.object(gp, 'DUPLICATE_ENFORCEMENT_MODE', 'off'))
        self.enterContext(patch.object(gp, 'PRIMARY_LLM_PROVIDER', 'grok'))
        self.enterContext(patch.object(gp, 'LLM_FALLBACK_PROVIDER', 'disabled'))
        self.enterContext(patch.object(
            gp, 'get_db_connection', side_effect=AssertionError('unexpected article database')),
        )
        self.enterContext(patch.object(
            gp, 'OpenAI', side_effect=AssertionError('unexpected provider SDK call')),
        )

    def use_passages(self, *matches: tuple[SemanticCandidate, SourcePassage]) -> None:
        '''Return only the passage rows matched to the existing retrieval fixture.'''
        self.repository.get_source_passages.return_value = {
            (candidate.candidate_document_id, candidate.best_candidate_chunk_index): passage
            for candidate, passage in matches
        }
        self.source_snapshots = {
            passage.document.source_article_id: SourceArticleSnapshot(
                title=passage.document.title, full_text=archive_body(passage.chunk_text),
                news_url=passage.document.source_url, published_at=passage.document.published_at,
                inserted_at=candidate.published_at + timedelta(minutes=1),
            )
            for candidate, passage in matches
        }
        self.raw_repository.get_source_article_snapshot.side_effect = self.source_snapshots.get

    def process(
        self, mode: str, raw: dict[str, Any],
        result: SemanticShadowRunResult | None, repaired: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        '''Run the production entry point with fixed rewrite and structural repair outputs.'''
        final = writer_document(raw) if repaired is None else repaired
        draft = {**final, 'full_text': final['full_text'].replace('<h2>What the operators said</h2>', '')}

        def fake_provider(_provider: str, _model: str, *, phase: str, **_kwargs: Any) -> str:
            self.assertIn(phase, ('rewrite', 'repair'))
            return json.dumps(draft if phase == 'rewrite' else final)

        self.semantic.return_value = result
        self.writer.side_effect = fake_provider
        with patch.object(gp, 'RAG_CONTEXT_MODE', mode), self.assertLogs(level='INFO') as logs:
            self.assertIs(gp.process_one(raw), True)

        stored, source = self.store.call_args.args
        self.assertEqual(source, raw)
        self.assertEqual(self.mark_processed.call_args.args, (raw['news_url'],))
        return stored, '\n'.join(log for log in logs.output if '[RAG-CONTEXT]' in log)

    def publish(
        self, document: dict[str, Any], raw: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        '''Capture the production WordPress POST body and registered SEO values.'''
        article = PublicationArticle.from_mapping(document)
        context = PublicationContext(
            published_at_utc=QUERY_TIME.replace(tzinfo=UTC), creation_is_fresh=True,
            raw_article_id=raw['id'], rich_article_id=901, source_url=raw['news_url'],
        )
        response = SimpleNamespace(status_code=201, json=lambda: {'id': 1001})
        with (
            patch.object(publisher, 'PUBLISH_DAILY_HARD_CAP_ENABLED', False),
            patch.object(publisher, 'MARKET_REFERRAL_URLS', {'Coinbase': REFERRAL_URL}),
            patch.object(publisher, '_CUSTOM_REFERRALS', ()),
            patch.object(publisher, 'ensure_category', return_value=22),
            patch.object(publisher, 'ensure_term', return_value=33),
            patch.object(publisher, 'write_yoast_metadata') as seo,
            patch.object(publisher.session, 'post', return_value=response) as post,
            patch.object(publisher.session, 'get', side_effect=AssertionError('unexpected WordPress GET')),
            patch('builtins.print'),
        ):
            result = publisher.WordPressPublisher().publish(article, None, context)
        self.assertTrue(result.success)
        self.assertTrue(result.created)
        post.assert_called_once()
        self.assertTrue(post.call_args.args[0].endswith('/wp-json/wp/v2/posts'))
        seo.assert_called_once()
        return post.call_args.kwargs['json'], seo.call_args.kwargs

    def test_off_and_shadow_preserve_writer_calls_storage_and_wordpress_payload(self) -> None:
        raw = current_article()
        match = indexed_passage()
        self.use_passages(match)
        retrieval = semantic_result(match[0])

        off, off_log = self.process('off', raw, retrieval)
        self.vector.assert_not_called()
        self.repository.get_source_passages.assert_not_called()
        self.raw_repository.get_source_article_snapshot.assert_not_called()
        self.semantic.assert_called_once_with(raw)
        self.assertEqual(off_log, '')
        off_calls = self.writer.call_args_list.copy()
        self.assertEqual([call.kwargs['phase'] for call in off_calls], ['rewrite', 'repair'])
        self.writer.reset_mock()

        shadow, shadow_log = self.process('shadow', raw, retrieval)
        self.assertEqual(shadow, off)
        self.assertEqual(self.writer.call_args_list, off_calls)
        self.assertEqual(self.research.call_count, 2)
        self.assertEqual(self.semantic.call_count, 2)
        self.vector.assert_called_once()
        self.repository.get_source_passages.assert_called_once_with(((501, 2),), embedding_version=EMBEDDING_VERSION)
        self.raw_repository.get_source_article_snapshot.assert_called_once_with(81)
        self.repository.nearest_chunks.assert_not_called()
        self.assertIn('embeddings_ready=True', shadow_log)
        self.assertIn('decision=would_apply', shadow_log)
        self.assertIn('reason=suggested', shadow_log)
        self.assertNotIn(PAUSE_PASSAGE, shadow_log)
        self.assertEqual(self.publish(shadow, raw), self.publish(off, raw))

    def test_apply_inserts_one_dated_citation_and_preserves_final_public_payload(self) -> None:
        raw = current_article()
        match = indexed_passage()
        self.use_passages(match)
        snapshot = self.source_snapshots[81]
        assert snapshot is not None and snapshot.full_text is not None
        paragraphs = snapshot.full_text.split('\n\n')
        equivalent_html = ''.join(
            f'<p>{paragraph}</p>' for paragraph in paragraphs
        ).replace(' ', '  ')
        self.source_snapshots[81] = replace(snapshot, full_text=equivalent_html)
        retrieval = semantic_result(match[0])
        off, _ = self.process('off', raw, retrieval)
        before, seo_before = self.publish(off, raw)

        applied, applied_log = self.process('apply', raw, retrieval)
        expected = (
            f'On November 25, 2026, <a href="{ARCHIVE_URL}">archive.example.invalid</a> reported that '
            'Meridian Vault had paused withdrawals on November 24, 2026.'
        )
        self.assertEqual(applied['full_text'].count(expected), 1)
        paragraph = f'\n<p>{expected}</p>'
        self.assertEqual(applied['full_text'].replace(paragraph, '', 1), off['full_text'])
        self.assertTrue(applied['full_text'].startswith(LEAD))
        self.assertEqual({**applied, 'full_text': off['full_text']}, off)
        self.assertIn(f'<a href="{SOURCE_URL}">Current report</a>', applied['full_text'])
        self.assertNotIn(ARCHIVE_URL, str(self.writer.call_args_list))
        self.assertNotIn(PAUSE_PASSAGE, str(self.writer.call_args_list))
        self.assertIn('decision=applied', applied_log)
        self.assertIn('reason=suggested', applied_log)

        after, seo_after = self.publish(applied, raw)
        self.assertEqual(after['content'].count(expected), 1)
        self.assertEqual(after['content'].replace(paragraph, '', 1), before['content'])
        self.assertTrue(after['content'].startswith(LEAD))
        self.assertEqual({**after, 'content': before['content']}, before)
        self.assertEqual(seo_after, seo_before)
        self.assertIn(f'<a href="{SOURCE_URL}">Current report</a>', after['content'])
        self.assertIn(f'<a href="{REFERRAL_URL}" target="_blank" rel="noopener sponsored">Coinbase</a>', after['content'])

    def test_hypothetical_pause_is_omitted_in_shadow_and_final_wordpress_payload(self) -> None:
        raw = current_article()
        off, _ = self.process('off', raw, None)
        before = self.publish(off, raw)
        pause = 'Meridian Vault paused withdrawals on November 24, 2026.'
        for hypothetical in (
            f'The following scenario is part of a hypothetical tabletop exercise. {pause}',
            f'The following hypothetical tabletop exercise describes an emergency response scenario:\n\n{pause}',
            f'The following scenario is hypothetical.\n\n{pause}',
            f'{pause} That pause was part of a tabletop exercise.',
        ):
            match = indexed_passage(title='Meridian Vault tabletop exercise', text=hypothetical)
            self.use_passages(match)
            for mode in ('shadow', 'apply'):
                with self.subTest(mode=mode, hypothetical=hypothetical):
                    doc, logs = self.process(mode, raw, semantic_result(match[0]))
                    self.assertEqual(doc, off)
                    self.assertEqual(self.publish(doc, raw), before)
                    self.assertIn('decision=skipped reason=hypothetical_passage', logs)
        self.raw_repository.get_source_article_snapshot.assert_not_called()

    def test_real_pause_near_an_unrelated_drill_is_still_eligible(self) -> None:
        raw = current_article()
        off, _ = self.process('off', raw, None)
        for text in (
            f'An unrelated hypothetical tabletop exercise took place at another venue.\n\n{PAUSE_PASSAGE}',
            f'{PAUSE_PASSAGE} This week, an unrelated team held a tabletop exercise.',
        ):
            with self.subTest(text=text):
                match = indexed_passage(text=text)
                self.use_passages(match)
                retrieval = semantic_result(match[0])
                shadow, shadow_logs = self.process('shadow', raw, retrieval)
                self.assertEqual(shadow, off)
                self.assertIn('decision=would_apply reason=suggested', shadow_logs)

                applied, apply_logs = self.process('apply', raw, retrieval)
                body = self.publish(applied, raw)[0]['content']
                self.assertIn('decision=applied reason=suggested', apply_logs)
                self.assertEqual(body.count(ARCHIVE_URL), 1)
                self.assertIn('had paused withdrawals on November 24, 2026', body)
                self.assertNotIn('tabletop exercise', body)

    def test_superseded_passage_is_omitted_with_or_without_corrected_embedding(self) -> None:
        raw = current_article()
        old = indexed_passage()
        corrected = indexed_passage(document_id=502, text=CORRECTED_PASSAGE, distance=0.5)
        off, _ = self.process('off', raw, None)
        before = self.publish(off, raw)
        self.assertNotEqual(old[1].document.content_hash, corrected[1].document.content_hash)
        self.assertEqual(old[1].document.document_key, corrected[1].document.document_key)
        query = replace(
            old[1].document, id=700, document_key='source_article:101',
            source_article_id=raw['id'], title=raw['title'], source_url=raw['news_url'],
            published_at=raw['publish_date'],
        )
        self.repository.get_latest_source_document.return_value = query
        self.repository.get_document_embedding_job.return_value = SimpleNamespace(status='completed')
        self.repository.get_chunks.return_value = [
            SimpleNamespace(id=701, chunk_index=0, embedding_version=EMBEDDING_VERSION, embedding=(1.0,)),
        ]
        service = SemanticRetrievalService(
            repository=self.repository,
            settings=SemanticRetrievalSettings(True, True, EMBEDDING_VERSION),
        )

        for corrected_indexed in (False, True):
            stored = (old, corrected) if corrected_indexed else (old,)
            self.use_passages(*stored)
            archived = self.source_snapshots[81]
            assert archived is not None
            self.source_snapshots[81] = replace(archived, full_text=archive_body(CORRECTED_PASSAGE))
            self.repository.nearest_chunks.return_value = [
                VectorMatch(
                    distance=candidate.native_distance, document_id=passage.document.id,
                    document_key=passage.document.document_key, source_type=passage.document.source_type,
                    source_article_id=passage.document.source_article_id, rich_article_id=None,
                    source_url=passage.document.source_url, title=passage.document.title,
                    published_at=passage.document.published_at, chunk_id=passage.chunk_id,
                    chunk_index=passage.chunk_index, chunk_text=passage.chunk_text,
                    chunk_hash=passage.chunk_hash, embedding_model='fixture',
                    embedding_version=passage.embedding_version,
                )
                for candidate, passage in stored
            ]
            ranked = service.retrieve_source_neighbors(raw['id'])
            self.assertEqual(ranked.candidates, (old[0],))
            retrieval = semantic_result(*ranked.candidates)
            for mode in ('shadow', 'apply'):
                with self.subTest(mode=mode, corrected_indexed=corrected_indexed):
                    doc, logs = self.process(mode, raw, retrieval)
                    self.assertEqual(doc, off)
                    self.assertEqual(self.publish(doc, raw), before)
                    self.assertIn('decision=skipped reason=candidate_source_superseded', logs)
        self.assertEqual(self.repository.nearest_chunks.call_count, 2)

    def test_incompatible_liquidation_totals_and_shared_ticker_do_not_add_context(self) -> None:
        raw = {
            **current_article(), 'title': 'Market-wide liquidations total $691M in one hour',
            'full_text': 'Across the crypto market, $691M in positions were liquidated in one hour. BTC fell.',
        }
        prior = indexed_passage(
            raw=raw, title='BTC liquidations reached $912.53M over 24 hours',
            text='Bitcoin-only liquidations totaled $912.53M over 24 hours.',
        )
        ticker = indexed_passage(
            raw=raw, document_id=503, source_id=83, url='https://archive.example.invalid/bitcoin-miners',
            title='Bitcoin miners revise November production outlook',
            text='BTC miners reduced their production outlook and reviewed energy costs.',
        )
        self.use_passages(prior, ticker)
        generated = writer_document(raw, lead='<p>Market-wide liquidations reached $691M in one hour.</p>')
        doc, logs = self.process('apply', raw, semantic_result(prior[0], ticker[0], raw=raw), generated)
        self.assertEqual(doc, generated)
        self.assertIn('$691M in one hour', doc['full_text'])
        self.assertNotIn('$912.53M', doc['full_text'])
        self.assertNotIn(ARCHIVE_URL, doc['full_text'])
        self.assertIn('reason=no_specific_connection', logs)
        self.repository.get_source_passages.assert_not_called()

    def test_related_event_with_structured_contradiction_omits_even_matched_vault(self) -> None:
        raw = current_article()
        raw['full_text'] += ' The Federal Reserve held its federal funds target range at 3.75%-4.00%.'
        prior_text = PAUSE_PASSAGE + ' The Federal Reserve held its federal funds target range at 4.25%-4.50%.'
        prior = indexed_passage(text=prior_text, raw=raw)
        assessment = assess_relationship(raw, {'full_text': prior_text, 'title': prior[0].candidate_title})
        self.assertIs(assessment.assessment_type, AssessmentType.RELATED_EVENT)
        self.assertIn('conflicting_rate_range', assessment.contradiction_flags)
        self.use_passages(prior)

        doc, logs = self.process('apply', raw, semantic_result(prior[0], raw=raw))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('contradictory_evidence', logs)

    def test_metadata_provenance_and_citation_boundaries(self) -> None:
        raw = current_article()
        for changes, reason in (
            ({'source_id': raw['id']}, 'self_match'),
            ({'url': raw['news_url'] + '?utm_source=rss'}, 'self_match'),
            ({'published': QUERY_TIME}, 'candidate_not_earlier'),
            ({'published': QUERY_TIME + timedelta(seconds=1)}, 'candidate_not_earlier'),
            ({'source_type': SourceType.COINCOURIER_GENERATED}, 'generated_candidate'),
            ({'url': None}, 'missing_source_url'),
            ({'url': 'https://reader:secret@archive.example.invalid/claimed-fact'}, 'unsafe_source_url'),
        ):
            with self.subTest(reason=reason, changes=changes):
                match = indexed_passage(**changes)
                self.use_passages(match)
                doc, logs = self.process('apply', raw, semantic_result(match[0]))
                self.assertEqual(doc, writer_document(raw))
                self.assertIn(reason, logs)
                self.assertNotIn('secret', logs)
        self.repository.get_source_passages.assert_not_called()

    def test_duplicate_and_syndicated_reports_are_omitted(self) -> None:
        raw = current_article()
        syndication = indexed_passage(text=PAUSE_PASSAGE + ' This story originally appeared in Example Wire.')
        self.use_passages(syndication)
        doc, logs = self.process('apply', raw, semantic_result(syndication[0]))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('syndicated_report', logs)

        prior = indexed_passage()
        syndicated_copy = indexed_passage(
            document_id=502, source_id=82, url='https://copy.example.invalid/meridian-paused',
        )
        self.use_passages(prior, syndicated_copy)
        doc, logs = self.process('apply', raw, semantic_result(prior[0], syndicated_copy[0]))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('syndicated_report', logs)

        syndicated_pair = (
            indexed_passage(title='Operator pauses withdrawals', text='Operator pauses withdrawals\n\n' + PAUSE_PASSAGE),
            indexed_passage(
                document_id=502, source_id=82, url='https://copy.example.invalid/meridian-paused',
                title='Vault temporarily halts withdrawals', text='Vault temporarily halts withdrawals\n\n' + PAUSE_PASSAGE,
            ),
        )
        self.use_passages(*syndicated_pair)
        doc, logs = self.process('apply', raw, semantic_result(*(item[0] for item in syndicated_pair)))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('syndicated_report', logs)

        duplicate = indexed_passage(title=raw['title'], text=raw['full_text'])
        self.use_passages(duplicate)
        doc, logs = self.process('apply', raw, semantic_result(duplicate[0]))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('duplicate_report', logs)

    def test_prior_source_retells_the_resumption_with_the_same_pause(self) -> None:
        raw = current_article()
        other = indexed_passage(
            title='Withdrawal operations return after disruption',
            text='Meridian Vault resumed withdrawals after completing a review. ' + PAUSE_PASSAGE,
        )
        self.use_passages(other)
        doc, logs = self.process('apply', raw, semantic_result(other[0]))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('duplicate_report', logs)

    def test_uncertain_passage_and_an_existing_fact_in_repaired_draft_are_omitted(self) -> None:
        raw = current_article()
        for qualification in (
            'The report remained unconfirmed.',
            'That narrative was debunked by its operators.',
            'Correction: withdrawals remained open.',
        ):
            with self.subTest(qualification=qualification):
                uncertain = indexed_passage(text=f'{PAUSE_PASSAGE} {qualification}')
                self.use_passages(uncertain)
                doc, logs = self.process('apply', raw, semantic_result(uncertain[0]))
                self.assertEqual(doc, writer_document(raw))
                self.assertIn('uncertain_evidence', logs)

        prior = indexed_passage()
        self.use_passages(prior)
        repaired = writer_document(raw)
        repaired['full_text'] += '<p>Meridian Vault suspended withdrawals on November 24, 2026.</p>'
        doc, logs = self.process('apply', raw, semantic_result(prior[0]), repaired)
        self.assertEqual(doc, repaired)
        self.assertIn('fact_already_in_draft', logs)

    def test_missing_embedding_store_outage_and_unverified_exact_passage_fail_open(self) -> None:
        raw = current_article()
        missing_embedding = semantic_result(
            status=SemanticAssessmentStatus.NOT_READY, retrieval_status=SemanticRetrievalStatus.QUERY_NOT_READY,
            reason='query_embedding_incomplete',
        )
        doc, logs = self.process('apply', raw, missing_embedding)
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('embeddings_ready=False', logs)
        self.assertIn('query_embedding_incomplete', logs)
        self.repository.get_source_passages.assert_not_called()

        match = indexed_passage()
        for passage in ({}, {(501, 2): replace(match[1], chunk_id=999)}):
            with self.subTest(passage_found=bool(passage)):
                self.repository.get_source_passages.return_value = passage
                doc, logs = self.process('apply', raw, semantic_result(match[0]))
                self.assertEqual(doc, writer_document(raw))
                self.assertIn('passage_mismatch' if passage else 'missing_passage', logs)

        self.repository.get_source_passages.side_effect = OSError('archive-private-failure')
        doc, logs = self.process('apply', raw, semantic_result(match[0]))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('reason=passage_read_unavailable', logs)
        self.assertNotIn('archive-private-failure', logs)
        self.assertNotIn('suggested', logs)

    def test_ranked_shortlist_reads_once_and_retries_do_not_leak_context(self) -> None:
        raw = current_article()
        first = indexed_passage(distance=0.01)
        second_url = 'https://backup.example.invalid/meridian-paused'
        second = indexed_passage(
            document_id=504, source_id=84, url=second_url, distance=0.05,
            text='Meridian Vault suspended withdrawals on November 24, 2026 following a pricing oracle disruption.',
        )
        self.use_passages(first, second)
        retrieval = semantic_result(second[0], first[0])

        initial, _ = self.process('apply', raw, retrieval)
        self.repository.get_source_passages.assert_called_once()
        self.assertEqual(
            set(self.repository.get_source_passages.call_args.args[0]), {(501, 2), (504, 2)},
        )
        self.assertEqual(initial['full_text'].count(ARCHIVE_URL), 1)
        self.assertNotIn(second_url, initial['full_text'])

        repeated, _ = self.process('apply', raw, retrieval, initial)
        self.assertEqual(repeated, initial)

        other = {
            **raw, 'id': 102, 'title': 'Another Vault resumes withdrawals',
            'news_url': 'https://current.example.invalid/another-resumes',
            'full_text': 'Another Vault resumed withdrawals on November 26, 2026.',
        }
        for result in (retrieval, None):
            with self.subTest(stale_retrieval=result is not None):
                doc, _ = self.process('apply', other, result)
                self.assertEqual(doc, writer_document(other))

    def test_duplicate_enforcement_stops_before_any_passage_read(self) -> None:
        raw = current_article()
        match = indexed_passage()
        self.use_passages(match)
        self.semantic.return_value = semantic_result(match[0])
        allow = DuplicateDecision(mode='enforce', article_id=raw['id'])
        reject = replace(allow, reject=True, canonical_raw_id=match[0].candidate_source_article_id)
        with (
            patch.object(gp, 'RAG_CONTEXT_MODE', 'apply'),
            patch.object(gp, 'DUPLICATE_ENFORCEMENT_MODE', 'enforce'),
            patch.object(gp, '_duplicate_enforcement_fail_open', side_effect=[allow, reject]) as duplicate,
        ):
            outcome = gp.process_one(raw, mark_complete=False)

        self.assertIsInstance(outcome, gp.ProcessingDuplicate)
        self.semantic.assert_called_once_with(raw)
        self.assertEqual(duplicate.call_count, 2)
        self.assertEqual(duplicate.call_args.kwargs['semantic_candidates'], (match[0],))
        self.repository.get_source_passages.assert_not_called()
        self.research.assert_not_called()
        self.writer.assert_not_called()
        self.store.assert_not_called()

    def test_current_ingestion_fallback_time_skips_before_passage_reads(self) -> None:
        for ingested_at in (None, QUERY_TIME, QUERY_TIME - timedelta(seconds=1)):
            with self.subTest(ingested_at=ingested_at):
                raw = {**current_article(), 'insertDate': ingested_at}
                match = indexed_passage(raw=raw)
                self.use_passages(match)
                doc, logs = self.process('apply', raw, semantic_result(match[0], raw=raw))
                self.assertEqual(doc, writer_document(raw))
                self.assertIn('reason=query_publication_unverified', logs)
        self.repository.get_source_passages.assert_not_called()
        self.raw_repository.get_source_article_snapshot.assert_not_called()

    def test_archive_requires_matching_publication_before_ingestion(self) -> None:
        raw = current_article()
        match = indexed_passage()
        self.use_passages(match)
        archived = self.source_snapshots[81]
        assert archived is not None
        for dates in (
            {'published_at': None},
            {'inserted_at': REPORT_TIME},
            {'inserted_at': REPORT_TIME - timedelta(seconds=1)},
            {'published_at': REPORT_TIME - timedelta(seconds=1)},
        ):
            with self.subTest(dates=dates):
                self.source_snapshots[81] = replace(archived, **dates)
                doc, logs = self.process('apply', raw, semantic_result(match[0]))
                self.assertEqual(doc, writer_document(raw))
                self.assertIn('reason=candidate_publication_unverified', logs)

        self.source_snapshots[81] = None
        doc, logs = self.process('apply', raw, semantic_result(match[0]))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('reason=candidate_source_unverified', logs)

        self.raw_repository.get_source_article_snapshot.side_effect = OSError('private-ingest-error')
        doc, logs = self.process('apply', raw, semantic_result(match[0]))
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('reason=candidate_source_read_unavailable', logs)
        self.assertNotIn('private-ingest-error', logs)

    def test_context_that_would_overflow_the_text_column_is_discarded(self) -> None:
        raw = current_article()
        match = indexed_passage()
        self.use_passages(match)
        repaired = writer_document(raw)
        remaining = 65_500 - len(repaired['full_text'].encode('utf-8')) - len('<p></p>')
        extra = 'é' * (remaining // 2) + 'x' * (remaining % 2)
        repaired['full_text'] += f'<p>{extra}</p>'
        self.assertEqual(len(repaired['full_text'].encode('utf-8')), 65_500)
        self.assertLess(len(repaired['full_text']), 65_000)
        for mode in ('shadow', 'apply'):
            with self.subTest(mode=mode):
                doc, logs = self.process(mode, raw, semantic_result(match[0]), repaired)
                self.assertEqual(doc, repaired)
                self.assertIn('reason=draft_storage_limit', logs)

    def test_invalid_optional_insertion_keeps_the_original_draft(self) -> None:
        raw = current_article()
        match = indexed_passage()
        self.use_passages(match)
        validate_original = gp.hard_article_validation_failures
        checked: list[bool] = []

        def validate(doc: dict[str, Any], source: dict[str, Any]) -> list[str]:
            attached = ARCHIVE_URL in doc['full_text']
            checked.append(attached)
            if attached:
                return ['invalid_optional_context']
            return validate_original(doc, source)

        with patch.object(gp, 'hard_article_validation_failures', side_effect=validate):
            doc, logs = self.process('apply', raw, semantic_result(match[0]))
        self.assertIn(True, checked)
        self.assertEqual(doc, writer_document(raw))
        self.assertIn('reason=draft_validation_failed', logs)
