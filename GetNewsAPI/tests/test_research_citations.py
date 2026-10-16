'''Offline isolation and bounds for in-memory provider citation metadata.'''

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
from threading import Barrier
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from research_citations import (
    MAX_CITATION_SPAN_INDEX,
    ResearchCitation,
    current_research_citations,
    research_citation_scope,
    retain_response_citations,
)


PRIMARY = ResearchCitation('https://issuer.example.invalid/announcement')
SECONDARY = ResearchCitation('https://regulator.example.invalid/filing')


class ResearchCitationTests(unittest.TestCase):
    def test_no_metadata_retained_outside_explicit_scope(self):
        retain_response_citations((PRIMARY,))
        self.assertEqual(current_research_citations(), ())
        with research_citation_scope():
            retain_response_citations((PRIMARY,))
            self.assertEqual(current_research_citations(), (PRIMARY,))
        self.assertEqual(current_research_citations(), ())

    def test_scope_resets_after_exception_and_next_article_starts_empty(self):
        with self.assertRaisesRegex(RuntimeError, 'failed draft'):
            with research_citation_scope():
                retain_response_citations((PRIMARY,))
                raise RuntimeError('failed draft')
        with research_citation_scope():
            self.assertEqual(current_research_citations(), ())
            retain_response_citations((SECONDARY,))
            self.assertEqual(current_research_citations(), (SECONDARY,))
        self.assertEqual(current_research_citations(), ())

    def test_nested_scope_restores_outer_article_even_after_exception(self):
        with research_citation_scope():
            retain_response_citations((PRIMARY,))
            with self.assertRaises(ValueError):
                with research_citation_scope():
                    self.assertEqual(current_research_citations(), ())
                    retain_response_citations((SECONDARY,))
                    raise ValueError('nested failure')
            self.assertEqual(current_research_citations(), (PRIMARY,))

    def test_decorator_is_reusable_and_preserves_existing_function_contract(self):
        @research_citation_scope()
        def article(raw, extra, video=''):
            self.assertEqual(current_research_citations(), ())
            retain_response_citations((PRIMARY,))
            return raw, extra, video, current_research_citations()

        for _ in range(2):
            self.assertEqual(article(7, 'plain research', ''), (7, 'plain research', '', (PRIMARY,)))
            self.assertEqual(current_research_citations(), ())

    def test_concurrent_threads_cannot_exchange_article_permissions(self):
        barrier = Barrier(2)

        def process(citation):
            with research_citation_scope():
                self.assertEqual(current_research_citations(), ())
                retain_response_citations((citation,))
                barrier.wait(timeout=5)
                return current_research_citations()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(process, citation) for citation in (PRIMARY, SECONDARY)]
            self.assertEqual([future.result(timeout=10) for future in futures], [(PRIMARY,), (SECONDARY,)])
        self.assertEqual(current_research_citations(), ())

    def test_independent_context_has_no_parent_permissions(self):
        with research_citation_scope():
            retain_response_citations((PRIMARY,))
            self.assertEqual(Context().run(current_research_citations), ())
            self.assertEqual(current_research_citations(), (PRIMARY,))

    def test_normalized_deduplication_retains_at_most_three_sources(self):
        values = (
            ResearchCitation(PRIMARY.url + '?utm_source=snapi'), PRIMARY,
            SECONDARY, ResearchCitation('https://data.example.invalid/release'),
            ResearchCitation('https://fourth.example.invalid/extra'),
        )
        with research_citation_scope():
            retain_response_citations(values)
            self.assertEqual(
                tuple(citation.url for citation in current_research_citations()),
                (PRIMARY.url, SECONDARY.url, 'https://data.example.invalid/release'),
            )

    def test_rejects_untrusted_shapes_unsafe_urls_and_oversized_values(self):
        values = (
            {'url': PRIMARY.url}, PRIMARY.url,
            ResearchCitation('javascript:alert(1)'),
            ResearchCitation('https://user:password@example.invalid/secret'),
            ResearchCitation('https://en.wikipedia.org/wiki/Bitcoin'),
            ResearchCitation('https://issuer.example.invalid/' + 'a' * 1000),
            ResearchCitation(PRIMARY.url, title=None),
            PRIMARY,
        )
        with research_citation_scope():
            retain_response_citations(values)
            self.assertEqual(current_research_citations(), (PRIMARY,))
            retain_response_citations('Sources: ' + SECONDARY.url)
            self.assertEqual(current_research_citations(), ())

    def test_optional_metadata_is_bounded_without_growing_research_text(self):
        citation = ResearchCitation(PRIMARY.url, '  Issuer\n\t' + 'x' * 300, 4, 90)
        with research_citation_scope():
            retain_response_citations((citation,))
            accepted, = current_research_citations()
            self.assertEqual(accepted.url, PRIMARY.url)
            self.assertEqual(len(accepted.title), 200)
            self.assertTrue(accepted.title.startswith('Issuer '))
            self.assertNotIn('\n', accepted.title)
            self.assertEqual((accepted.start_index, accepted.end_index), (4, 90))
            with self.assertRaises(FrozenInstanceError):
                accepted.url = SECONDARY.url

    def test_rejects_invalid_spans_instead_of_attaching_misleading_metadata(self):
        invalid = ((None, 4), (0, None), (True, 3), (0, 2.0), (-1, 4), (3, 3), (4, 2),
                   (0, MAX_CITATION_SPAN_INDEX + 1))
        with research_citation_scope():
            for start, end in invalid:
                with self.subTest(start=start, end=end):
                    retain_response_citations((ResearchCitation(PRIMARY.url, '', start, end),))
                    self.assertEqual(current_research_citations(), ())

    def test_new_response_replaces_permissions_and_explicit_clear_removes_them(self):
        with research_citation_scope():
            retain_response_citations((PRIMARY,))
            retain_response_citations((SECONDARY,))
            self.assertEqual(current_research_citations(), (SECONDARY,))
            retain_response_citations(())
            self.assertEqual(current_research_citations(), ())


if __name__ == '__main__':
    unittest.main()
