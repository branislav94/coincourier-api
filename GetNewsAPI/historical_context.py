'''Select one historical fact from the exact archived source passage.'''

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import escape
from typing import Any, Protocol
from urllib.parse import urlsplit

from mysql.connector import Error as DatabaseError

from article_source_urls import normalize_source_url
from duplicate_detection.identities import canonicalize_url, normalize_source_content
from duplicate_detection.lexical import token_set_jaccard
from duplicate_detection.policy import AssessmentType, assess_relationship
from embeddings.chunking import normalize_text
from semantic_retrieval.models import MAX_SEMANTIC_TOP_K, SemanticCandidate, SemanticRetrievalStatus
from semantic_retrieval.shadow import SemanticShadowRunResult
from vector_store.models import SourcePassage, SourceType
from vector_store.repository import VectorRepository


_VAULT_NAME = r'(?:[A-Z][A-Za-z0-9-]{1,30} ){1,4}(?i:vault)'
_GENERIC_NAME_WORDS = frozenset(
    'a an the new old current original bitcoin btc ether ethereum eth solana sol '
    'usdc usdt xrp bnb crypto defi yield lending savings staking liquidity vault'.split()
)
_DATE_TEXT = r'(?:[A-Za-z]{3,9} [0-9]{1,2}, [0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})'
_RESUMPTION = re.compile(
    rf'^(?i:the )?(?P<vault>{_VAULT_NAME}) '
    r'(?i:(?:has )?(?:resume[sd]|reopen(?:ed|s)|restor(?:ed|es)) withdrawals'
    r'|withdrawals (?:have )?(?:resumed|reopened)'
    r'|withdrawals (?:were |have been )(?:restored|reopened))(?=[ .,:]|$)'
)
_PAUSE = re.compile(
    rf'^(?i:the )?(?P<vault>{_VAULT_NAME}) '
    r'(?i:(?:had )?(?:paused|suspended|halted|froze) withdrawals'
    r'|withdrawals (?:were |had been )(?:paused|suspended|halted|frozen))'
    rf'(?: on (?P<event_date>{_DATE_TEXT}))?'
    r'(?: (?:after|following|because|due to) [^<>]{1,160})?\.$'
)
_SENTENCE_BREAK = re.compile(r'\n+|(?<=[.!?])\s+')
_VAULT_MENTION = re.compile(rf'\b(?i:the )?(?P<vault>{_VAULT_NAME})\b')
_PAUSE_WORDS = re.compile(
    r'\b(?:paus(?:e[ds]?|ing)|suspen[ds]\w*|halt\w*|fro(?:ze|zen)|freez\w*|stopp\w*|'
    r'disabled|blocked|clos(?:ed|ure)|unavailable)\b'
)
_SYNDICATION = re.compile(
    r'\b(?:originally (?:published|appeared|reported)|first published (?:by|in|on)|'
    r'republish(?:ed|ing)|reprinted|syndicated|reproduced with permission|'
    r'this (?:article|story) (?:first appeared|was (?:published|provided) by))\b',
    re.IGNORECASE,
)
_UNCERTAIN_PASSAGE = re.compile(
    r'\b(?:reportedly|apparently|supposedly|unverified|unconfirmed|rumou?red|'
    r'claimed|incorrect|false|misreported|retracted|according to|could have|may have|might have|'
    r'denied|debunked|correction|never|did not|not suspended|no (?:withdrawal )?pause|'
    r'withdrawals? (?:remained|stayed|continued to be) open)\b',
    re.IGNORECASE,
)
_PUBLIC_HOST = re.compile(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:[a-z]{2,}|xn--[a-z0-9-]+)')
_MONTHS = (
    'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December',
)


class SourcePassageReader(Protocol):
    '''Read only the exact indexed source passages needed by this evaluation.'''

    def get_source_passages(
        self, matches: Sequence[tuple[int, int]], *, embedding_version: str,
    ) -> dict[tuple[int, int], SourcePassage]:
        ...


@dataclass(frozen=True)
class HistoricalContextFact:
    '''A past withdrawal pause stated directly in the linked archive passage.'''

    source_url: str
    report_time: datetime
    vault: str
    event_date: date | None
    chunk_id: int
    chunk_hash: str

    @property
    def report_date(self) -> date:
        '''Use the indexed publication date in UTC for the attribution.'''
        return self.report_time.date()

    def sentence_html(self) -> str:
        '''Render only the fixed sentence template and a checked source link.'''
        if normalize_historical_source_url(self.source_url) != self.source_url:
            raise ValueError('historical context citation is invalid')
        host = urlsplit(self.source_url).hostname or ''
        citation = f'<a href="{escape(self.source_url, quote=True)}">{escape(host)}</a>'
        event_date = f' on {_display_date(self.event_date)}' if self.event_date else ''
        return (
            f'On {_display_date(self.report_date)}, {citation} reported that '
            f'{escape(self.vault)} had paused withdrawals{event_date}.'
        )


@dataclass(frozen=True)
class HistoricalContextRejection:
    '''One candidate’s identifiers and content-free decision codes.'''

    candidate_document_id: int
    candidate_source_article_id: int
    chunk_index: int
    reason: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoricalContextDecision:
    '''Optional suggestion and the evidence locators used in the shadow pilot.'''

    reason: str
    fact: HistoricalContextFact | None = None
    candidate_document_id: int | None = None
    candidate_source_article_id: int | None = None
    chunk_index: int | None = None
    rejections: tuple[HistoricalContextRejection, ...] = ()


def normalize_historical_source_url(value: object) -> str | None:
    '''Require a public-facing article URL suitable for a reader citation.'''
    url = normalize_source_url(value, allow_sensitive_query=False)
    if url is None:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or '').rstrip('.').encode('idna').decode('ascii')
    if (
        not _PUBLIC_HOST.fullmatch(host)
        or host.endswith(('.localhost', '.local', '.localdomain', '.internal', '.intranet', '.lan', '.home', '.corp'))
        or host == 'coincourier.io'
        or host.endswith('.coincourier.io')
        or parts.port not in {None, 80 if parts.scheme.lower() == 'http' else 443}
        or not parts.path.strip('/')
    ):
        return None
    return url


def _display_date(value: date) -> str:
    return f'{_MONTHS[value.month - 1]} {value.day}, {value.year}'


def _parse_event_date(value: str) -> date | None:
    try:
        if value[0].isdigit():
            return date.fromisoformat(value)
        month_text, day_text, year_text = value.split(' ')
        month = next(
            (index for index, name in enumerate(_MONTHS, 1)
             if month_text.lower() in {name.lower(), name[:3].lower()}),
            None,
        )
        return date(int(year_text), month, int(day_text.rstrip(','))) if month else None
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _source_timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, datetime):
            return _utc(value)
        if isinstance(value, str) and len(value) >= 16 and value[10] in {'T', ' '}:
            return _utc(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        return None
    return None


def verified_source_publication_time(published: object, ingested: object) -> datetime | None:
    '''Require a publication time that predates the ingest batch timestamp.

    The fetcher falls back to its clock when the upstream date is missing.
    Those timestamps are greater than or equal to the batch's insertDate.
    '''
    publication = _source_timestamp(published)
    ingestion = _source_timestamp(ingested)
    if publication is None or ingestion is None or publication >= ingestion:
        return None
    return publication


def _current_vault(article: Mapping[str, Any]) -> str | None:
    body = normalize_text(str(article.get('text') or article.get('full_text') or ''))
    lead = _SENTENCE_BREAK.split(body, maxsplit=1)[0]
    match = _RESUMPTION.match(lead)
    if match is None or _UNCERTAIN_PASSAGE.search(lead):
        return None
    vault = match.group('vault').lower()
    if not set(vault.split()) - _GENERIC_NAME_WORDS:
        return None
    title = normalize_text(str(article.get('title') or ''))
    title_vaults = {part.group('vault').lower() for part in _VAULT_MENTION.finditer(title)}
    lead_vaults = {part.group('vault').lower() for part in _VAULT_MENTION.finditer(lead)}
    return vault if title_vaults == lead_vaults == {vault} else None


def _candidate_reason(
    candidate: SemanticCandidate,
    semantic: SemanticShadowRunResult,
    current_url: str,
    current_time: datetime,
) -> str | None:
    if (
        candidate.candidate_source_article_id == semantic.query_source_article_id
        or candidate.candidate_document_id == semantic.query_document_id
    ):
        return 'self_match'
    if candidate.source_type is not SourceType.SOURCE_ARTICLE:
        return 'generated_candidate'
    if (
        candidate.query_source_article_id != semantic.query_source_article_id
        or candidate.query_document_id != semantic.query_document_id
        or candidate.embedding_version != semantic.embedding_version
        or candidate.candidate_document_id <= 0
        or candidate.candidate_source_article_id <= 0
        or candidate.best_candidate_chunk_index < 0
        or not math.isfinite(candidate.native_distance)
    ):
        return 'candidate_mismatch'
    if candidate.published_at is None:
        return 'candidate_date_missing'
    if _utc(candidate.published_at) >= current_time:
        return 'candidate_not_earlier'
    if not candidate.candidate_source_url:
        return 'missing_source_url'
    url = normalize_historical_source_url(candidate.candidate_source_url)
    if url is None:
        return 'unsafe_source_url'
    if current_url and canonicalize_url(url) == current_url:
        return 'self_match'
    return None


def _reject(
    candidate: SemanticCandidate, reason: str, details: tuple[str, ...] = (),
) -> HistoricalContextRejection:
    return HistoricalContextRejection(
        candidate.candidate_document_id, candidate.candidate_source_article_id,
        candidate.best_candidate_chunk_index, reason, details,
    )


def _passage_matches(candidate: SemanticCandidate, passage: SourcePassage) -> bool:
    document = passage.document
    return not (
        document.source_type is not SourceType.SOURCE_ARTICLE
        or document.rich_article_id is not None
        or document.id != candidate.candidate_document_id
        or document.source_article_id != candidate.candidate_source_article_id
        or document.document_key != candidate.candidate_document_key
        or document.source_url != candidate.candidate_source_url
        or document.title != candidate.candidate_title
        or document.published_at is None
        or _utc(document.published_at) != _utc(candidate.published_at)
        or passage.embedding_version != candidate.embedding_version
        or passage.chunk_index != candidate.best_candidate_chunk_index
        or passage.chunk_id != candidate.best_candidate_chunk_id
        or passage.chunk_hash != candidate.best_candidate_chunk_hash
        or hashlib.sha256(passage.chunk_text.encode('utf-8')).hexdigest() != passage.chunk_hash
    )


def _passage_body(passage: SourcePassage) -> str:
    # The index may prepend the original headline to the first body chunk.
    title = normalize_text(passage.document.title)
    return passage.chunk_text.removeprefix(f'{title}\n\n') if title else passage.chunk_text


def _inspect_passage(
    candidate: SemanticCandidate,
    passage: SourcePassage,
    article: Mapping[str, Any],
    vault: str,
    draft: str,
) -> HistoricalContextFact | HistoricalContextRejection:
    document = passage.document
    chunk = passage.chunk_text
    previous = {
        'title': document.title, 'text': chunk, 'news_url': document.source_url,
        'publish_date': document.published_at,
    }
    assessment = assess_relationship(article, previous)
    if assessment.contradiction_flags:
        return _reject(candidate, 'contradictory_evidence', assessment.contradiction_flags)
    if (
        assessment.assessment_type in {AssessmentType.EXACT_DUPLICATE, AssessmentType.SAME_EVENT_DUPLICATE}
        or assessment.same_content_hash
        or token_set_jaccard(article.get('text') or article.get('full_text'), chunk) >= 0.90
    ):
        return _reject(candidate, 'duplicate_report')
    if _SYNDICATION.search(f'{document.title}\n{chunk}'):
        return _reject(candidate, 'syndicated_report')
    # The prior-only action is expected in a documented pause followed by a resumption.
    uncertainty = tuple(
        code for code in assessment.evidence_uncertainty
        if code != 'prior_only_action_evidence'
    )
    if uncertainty or _UNCERTAIN_PASSAGE.search(chunk):
        return _reject(candidate, 'uncertain_evidence', uncertainty)
    sentences = _SENTENCE_BREAK.split(f'{document.title}\n{chunk}')
    if any(
        (resumption := _RESUMPTION.match(sentence.strip()))
        and resumption.group('vault').lower() == vault
        for sentence in sentences
    ):
        return _reject(candidate, 'duplicate_report')
    if document.published_at is None:
        return _reject(candidate, 'candidate_date_missing')
    report_time = _utc(document.published_at)
    for sentence in _SENTENCE_BREAK.split(_passage_body(passage)):
        sentence = sentence.strip()
        match = _PAUSE.fullmatch(sentence)
        if match is None or match.group('vault').lower() != vault:
            continue
        date_text = match.group('event_date')
        event_date = _parse_event_date(date_text) if date_text else None
        if date_text and (event_date is None or event_date > report_time.date()):
            return _reject(candidate, 'event_date_unverified')
        # A reference to an earlier withdrawal pause is enough to skip the
        # suggestion even when the draft describes the same fact differently.
        if _PAUSE_WORDS.search(draft) and ('withdraw' in draft or vault in draft):
            return _reject(candidate, 'fact_already_in_draft')
        source_url = normalize_historical_source_url(document.source_url)
        if source_url is None:
            return _reject(candidate, 'unsafe_source_url')
        return HistoricalContextFact(
            source_url, report_time, match.group('vault'), event_date,
            passage.chunk_id, passage.chunk_hash,
        )
    return _reject(candidate, 'unsupported_passage')


def select_historical_context(
    article: Mapping[str, Any],
    draft_body: str,
    semantic: SemanticShadowRunResult | None,
    *,
    repository: SourcePassageReader | None = None,
) -> HistoricalContextDecision:
    '''Reuse the existing retrieval result and fail open on unavailable evidence.'''
    if semantic is None:
        return HistoricalContextDecision('semantic_result_unavailable')
    if semantic.retrieval_status not in {
        SemanticRetrievalStatus.RETRIEVED, SemanticRetrievalStatus.NO_CANDIDATES,
    }:
        return HistoricalContextDecision(semantic.reason or 'semantic_result_unavailable')
    if not semantic.candidates:
        return HistoricalContextDecision('no_candidates')
    if str(article.get('id')) != str(semantic.query_source_article_id):
        return HistoricalContextDecision('query_article_mismatch')
    if semantic.query_published_at is None or semantic.query_document_id is None:
        return HistoricalContextDecision('query_publication_unverified')

    current_time = verified_source_publication_time(article.get('publish_date'), article.get('insertDate'))
    if current_time is None:
        return HistoricalContextDecision('query_publication_unverified')
    if current_time != _utc(semantic.query_published_at):
        return HistoricalContextDecision('query_publication_mismatch')
    if len(semantic.candidates) > MAX_SEMANTIC_TOP_K:
        return HistoricalContextDecision('candidate_bound_exceeded')

    current_url = canonicalize_url(normalize_source_url(article.get('news_url')))
    draft = normalize_source_content(draft_body)
    vault = _current_vault(article)
    candidates = sorted(
        semantic.candidates,
        key=lambda item: (
            item.native_distance if math.isfinite(item.native_distance) else math.inf,
            item.candidate_source_article_id, item.candidate_document_id, item.best_candidate_chunk_index,
        ),
    )
    shortlist: dict[SemanticCandidate, tuple[int, int]] = {}
    rejections: dict[SemanticCandidate, HistoricalContextRejection] = {}
    for candidate in candidates:
        reason = _candidate_reason(candidate, semantic, current_url, current_time)
        if reason is None and vault is None:
            reason = 'no_specific_connection'
        if reason is None:
            shortlist[candidate] = (candidate.candidate_document_id, candidate.best_candidate_chunk_index)
        else:
            rejections[candidate] = _reject(candidate, reason)
    passages: dict[tuple[int, int], SourcePassage] = {}
    if shortlist and vault is not None:
        try:
            passages = (repository if repository is not None else VectorRepository()).get_source_passages(
                tuple(shortlist.values()), embedding_version=semantic.embedding_version,
            )
        except (DatabaseError, OSError, RuntimeError):
            return HistoricalContextDecision(
                'passage_read_unavailable', rejections=tuple(rejections.values()),
            )

    verified: dict[SemanticCandidate, SourcePassage] = {}
    bodies: dict[SemanticCandidate, str] = {}
    for candidate, position in shortlist.items():
        passage = passages.get(position)
        if passage is None:
            rejections[candidate] = _reject(candidate, 'missing_passage')
        elif not _passage_matches(candidate, passage):
            rejections[candidate] = _reject(candidate, 'passage_mismatch')
        else:
            verified[candidate] = passage
            bodies[candidate] = normalize_source_content(_passage_body(passage))
    # Identical archived bodies across documents do not identify the original report.
    body_counts = Counter(bodies.values())
    for candidate in candidates:
        if candidate in rejections or vault is None:
            continue
        if body_counts[bodies[candidate]] > 1:
            rejections[candidate] = _reject(candidate, 'syndicated_report')
            continue
        result = _inspect_passage(candidate, verified[candidate], article, vault, draft)
        if isinstance(result, HistoricalContextFact):
            return HistoricalContextDecision(
                'suggested', fact=result,
                candidate_document_id=candidate.candidate_document_id,
                candidate_source_article_id=candidate.candidate_source_article_id,
                chunk_index=candidate.best_candidate_chunk_index,
                rejections=tuple(rejections.values()),
            )
        rejections[candidate] = result
    ranked_rejections = tuple(rejections[candidate] for candidate in candidates)
    first = ranked_rejections[0]
    return HistoricalContextDecision(
        first.reason,
        candidate_document_id=first.candidate_document_id,
        candidate_source_article_id=first.candidate_source_article_id,
        chunk_index=first.chunk_index,
        rejections=ranked_rejections,
    )
