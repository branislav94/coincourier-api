'''Bounded annotation provenance scoped to one article-generation invocation.

The existing enrichment API returns plain strings and its consumers keep their
positional contracts. This private context carries only provider annotation
metadata beside that text, without mutating source evidence or trusting URLs in
research prose. The context is inactive outside an explicit processing scope.
'''

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from article_source_urls import normalize_source_url


MAX_RESEARCH_CITATIONS = 3
MAX_CITATION_URL_CHARACTERS = 1000
MAX_CITATION_TITLE_CHARACTERS = 200
MAX_CITATION_SPAN_INDEX = 1_000_000


@dataclass(frozen=True)
class ResearchCitation:
    '''Provider annotation metadata; provenance does not establish factual truth.'''

    url: str
    title: str = ''
    start_index: int | None = None
    end_index: int | None = None


_citations: ContextVar[tuple[ResearchCitation, ...] | None] = ContextVar(
    'article_research_citations', default=None,
)


@contextmanager
def research_citation_scope() -> Iterator[None]:
    '''Isolate one article; also usable as a reusable function decorator.'''
    token = _citations.set(())
    try:
        yield
    finally:
        _citations.reset(token)


def current_research_citations() -> tuple[ResearchCitation, ...]:
    '''Return only accepted metadata for the current article, if scoped.'''
    return _citations.get() or ()


def retain_response_citations(citations: tuple[ResearchCitation, ...]) -> None:
    '''Replace scoped metadata after complete response acceptance; never parse text.

    The caller is responsible for reading actual provider annotations and checking
    annotation spans against their associated output text. This boundary adds
    type, size and URL checks and never enables collection outside a scope.
    '''
    if _citations.get() is None:
        return
    accepted: list[ResearchCitation] = []
    seen: set[str] = set()
    if not isinstance(citations, tuple):
        _citations.set(())
        return
    for citation in citations:
        if not isinstance(citation, ResearchCitation):
            continue
        if not isinstance(citation.url, str) or len(citation.url) > MAX_CITATION_URL_CHARACTERS:
            continue
        url = normalize_source_url(citation.url)
        if not url or len(url) > MAX_CITATION_URL_CHARACTERS or url in seen:
            continue
        if not isinstance(citation.title, str):
            continue
        start, end = citation.start_index, citation.end_index
        if start is not None or end is not None:
            if (
                type(start) is not int or type(end) is not int
                or not 0 <= start < end <= MAX_CITATION_SPAN_INDEX
            ):
                continue
        title = ' '.join(citation.title.split())[:MAX_CITATION_TITLE_CHARACTERS]
        accepted.append(ResearchCitation(url, title, start, end))
        seen.add(url)
        if len(accepted) == MAX_RESEARCH_CITATIONS:
            break
    _citations.set(tuple(accepted))
