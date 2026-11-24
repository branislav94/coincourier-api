'''Apply and log the stock subject rule without changing shadow selection.'''
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from typing import Any, Protocol

from .subject import SubjectCandidate, assess_subject


logger = logging.getLogger(__name__)


class StockSubjectCandidate(SubjectCandidate, Protocol):
    '''Fields needed to correlate assessments across both stock engines.'''

    provider: str
    asset_key: str

    @property
    def asset_id(self) -> str:
        '''Return the provider asset identifier when available.'''
        ...


def article_correlation_id(article: dict[str, Any]) -> str:
    '''Return the stable, existing V2 correlation ID for an article.'''
    identity = article.get('id') or article.get('news_url') or article.get('url') or article.get('title') or 'unknown'
    return hashlib.sha256(str(identity).encode('utf-8', errors='replace')).hexdigest()[:12]


def _log_identifier(value: str) -> str:
    '''Keep IDs joinable without logging free text, URLs, or query strings.'''
    if re.fullmatch(r'[a-zA-Z0-9_:-]{1,100}', value):
        return value
    return hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:12]


def subject_rejection_reason(
    candidate: StockSubjectCandidate,
    article: dict[str, Any],
    *,
    mode: str,
    route: str,
    correlation: str,
) -> str:
    '''Record a confident mismatch and enforce it through the existing rejection reason.'''
    if mode == 'off':
        return ''
    try:
        assessment = assess_subject(article, candidate)
    except Exception:
        logger.warning('[IMG-SUBJECT] article=%s route=%s mode=%s reason=assessment_error', correlation, route, mode)
        return ''
    if not assessment.reason:
        return ''
    logger.info(
        '[IMG-SUBJECT] article=%s provider=%s asset_id=%s route=%s mode=%s decision=%s '
        'article_subject=%s image_subject=%s reason=%s',
        correlation, _log_identifier(candidate.provider),
        _log_identifier(candidate.asset_id or candidate.asset_key), route, mode,
        'reject' if mode == 'enforce' else 'would_reject',
        assessment.article_subject, assessment.image_subject, assessment.reason,
    )
    return 'context_mismatch' if mode == 'enforce' else ''


def rejected_subject_assets(
    candidates: Iterable[StockSubjectCandidate],
    article: dict[str, Any],
    *,
    mode: str,
    route: str,
    correlation: str,
) -> set[str]:
    '''Reject delivery variants of the same documented mismatch across fetched results.'''
    if mode == 'off':
        return set()
    return {
        candidate.asset_key for candidate in candidates
        if subject_rejection_reason(candidate, article, mode=mode, route=route, correlation=correlation)
    }
