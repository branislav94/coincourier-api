'''Assess clear subject conflicts using article and provider descriptions.'''
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .context import ORDINARY_WORD_PROJECTS, PROJECT_ALIASES, STRONG_CRYPTO_CONTEXT, _plain_text
from .queries import normalize_terms


_ASSET_ALIASES = {
    'bitcoin': {'bitcoin', 'btc'},
    'ethereum': {'ethereum', 'eth'},
    'xrp': {'xrp'},
    'tether': {'usdt'},
    'usd coin': {'usdc'},
    'bnb': {'bnb'},
    'dogecoin': {'dogecoin', 'doge'},
    'litecoin': {'litecoin', 'ltc'},
    'chainlink': {'chainlink'},
    'chainflip': {'chainflip'},
    'avalanche': {'avax'},
    'polygon': {'matic'},
    'stacks': {'stx'},
    'stellar': {'xlm'},
}
_AMBIGUOUS_ASSETS = {
    **PROJECT_ALIASES,
    'cyber': {'cyber'},
    'dash': {'dash'},
    'ethereum': {'ether'},
    'tether': {'tether'},
}
_KNOWN_ALIASES = frozenset().union(*_ASSET_ALIASES.values(), *_AMBIGUOUS_ASSETS.values())
_DISTINCT_ALIASES = frozenset().union(*_ASSET_ALIASES.values())
_ASSET_CUES = frozenset({'blockchain', 'coin', 'coins', 'cryptocurrency', 'crypto', 'ledger', 'protocol', 'token', 'tokens'})
_PAIR = re.compile(r'\b([a-z0-9]+)\s*/\s*([a-z0-9]+)\b', re.IGNORECASE)
_CASHTAG = re.compile(r'(?<!\w)\$([a-z][a-z0-9]{1,9})\b', re.IGNORECASE)
_TICKER = re.compile(r'(?<!\w)[A-Z][A-Z0-9]{1,9}(?!\w)')
_NAMED_ENTITY = re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b')
_CROSS_CHAIN = re.compile(r'\bcross[ -]chain\b', re.IGNORECASE)
_ASSET_COMPARISON = re.compile(
    r'\b(?:vs|versus|(?:out|under)perform\w*|compar(?:es?|ing|ed with|ed to)|relative to|trad(?:es|ing) against)\b',
    re.IGNORECASE,
)
_SECONDARY_CONTEXT = re.compile(r'\b(?:amid|against the backdrop of|following|despite)\b', re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r'(?<!\bvs)[.!?]\s', re.IGNORECASE)
_MARKET_TERMS = frozenset({'futures', 'liquidation', 'liquidations', 'market', 'markets', 'trading'})

# Only descriptions of concrete, unrelated subjects qualify. Words such as
# 'record', 'vault' and 'bridge' are insufficient on their own.
_LITERAL_SUBJECTS = {
    'religious_architecture': re.compile(r'\b(?:abbey|cloisters?|monaster(?:y|ies))\b'),
    'physical_bridge': re.compile(
        r'\b(?:footbridges?|(?:historic|pedestrian|suspension) bridges?'
        r'|bridges? .*\b(?:garden|park)|(?:garden|park) .*\bbridges?)\b'
    ),
    'record_player': re.compile(r'\b(?:record players?|turntables?|vinyl records?|vinyl .*\bmusic)\b'),
    'ammunition': re.compile(r'\b(?:ammunition|bullets? .*\bmagazines?|magazines? .*\bbullets?)\b'),
    'low_key_portrait': re.compile(r'\b(?:low key portraits?|portraits? .*\blow key)\b'),
    'laboratory': re.compile(r'\b(?:biochemistry|microscopes?|microscopy)\b'),
    'fruit': re.compile(r'\b(?:cherr(?:y|ies) .*\b(?:fruit|waterdrops?)|(?:fruits?|water|ripe|wet) .*\bcherr(?:y|ies))\b'),
}


class SubjectCandidate(Protocol):
    '''Provider description shared by modern and legacy candidates.'''

    @property
    def subject_text(self) -> str:
        '''Return descriptive provider text about the depicted subject.'''
        ...


@dataclass(frozen=True)
class SubjectAssessment:
    '''A clear rejection, with bounded labels for logs.'''

    reason: str = ''
    article_subject: str = ''
    image_subject: str = ''


def _visible_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ''
    try:
        return ' '.join(_plain_text(value[:limit * 8]).split())[:limit]
    except (AssertionError, ValueError):
        return ''  # Malformed HTML supplies no reliable subject evidence.


def subject_article_lead(article: dict[str, Any]) -> str:
    '''Retain a bounded visible lead for assessment during selection and rescue.'''
    for field in ('lead', 'excerpt', 'full_text'):
        lead = _visible_text(article.get(field), limit=600)
        if lead:
            return lead
    return ''


def _assets(text: str, *, tickers: frozenset[str] = frozenset()) -> set[str]:
    words = re.findall(r'[a-z0-9]+', text.lower())
    terms = set(words)
    uppercase = {ticker.lower() for ticker in _TICKER.findall(text)}
    assets = {asset for asset, aliases in _ASSET_ALIASES.items() if aliases & terms}
    qualified_names = set(tickers) | {ticker.lower() for ticker in _CASHTAG.findall(text)}
    for pair in _PAIR.findall(text.lower()):
        if _DISTINCT_ALIASES.intersection(pair):
            qualified_names.update(pair)
    # Require an asset phrase for ordinary words, with stronger evidence for
    # words that commonly appear just before a noun (e.g. near a coin).
    for left, right in zip(words, words[1:]):
        if left in _ASSET_CUES and (right not in ORDINARY_WORD_PROJECTS or right in uppercase):
            qualified_names.add(right)
        if right in _ASSET_CUES and (left not in ORDINARY_WORD_PROJECTS or left in uppercase or right == 'protocol'):
            qualified_names.add(left)
    if terms & _ASSET_CUES and re.search(r'\bdash logo\b', text, re.IGNORECASE):
        qualified_names.add('dash')
    if re.search(r'\bon solana\b', text, re.IGNORECASE) and 'beach' not in terms:
        qualified_names.add('solana')
    if 'cyber' in uppercase and (_CROSS_CHAIN.search(text) or 'solana' in qualified_names or 'minting' in terms):
        qualified_names.add('cyber')
    assets.update(
        asset for asset, aliases in _AMBIGUOUS_ASSETS.items()
        if aliases & terms & qualified_names
    )
    # A supported uppercase ticker can establish a new asset without expanding
    # the alias list. Bare ticker fields and general article tags cannot.
    assets.update((uppercase & tickers) - _KNOWN_ALIASES)
    return assets


def _literal_subjects(text: str) -> set[str]:
    normalized = ' '.join(re.findall(r'[a-z0-9]+', text.lower()))
    return {subject for subject, pattern in _LITERAL_SUBJECTS.items() if pattern.search(normalized)}


def _shared_named_entity(article_text: str, image_text: str) -> bool:
    return any(
        re.search(r'\b' + re.escape(name) + r'\b', image_text, re.IGNORECASE)
        for name in _NAMED_ENTITY.findall(article_text)
    )


def _subject_label(assets: set[str]) -> str:
    return ','.join(sorted(assets)[:6]) or 'crypto'


def assess_subject(article: dict[str, Any], candidate: SubjectCandidate) -> SubjectAssessment:
    '''Return a rejection only when the descriptive evidence supports a conflict.'''
    image = _visible_text(candidate.subject_text, limit=1200)
    if not image:
        return SubjectAssessment()
    headline = _visible_text(article.get('title'), limit=400)
    lead = subject_article_lead(article)
    lead_sentence = _SENTENCE_BREAK.split(lead, maxsplit=1)[0]
    tickers = frozenset(normalize_terms(str(article.get('tickers') or '')))
    article_assets = _assets(headline, tickers=tickers)
    headline_terms = set(normalize_terms(headline))
    crypto_headline = bool(headline_terms & STRONG_CRYPTO_CONTEXT or _CROSS_CHAIN.search(headline))
    if len(article_assets) > 1 and not _ASSET_COMPARISON.search(headline):
        primary_phrase = _SECONDARY_CONTEXT.split(headline, maxsplit=1)[0]
        article_assets = _assets(primary_phrase, tickers=tickers) or article_assets
    if not article_assets and (not headline or crypto_headline or headline_terms & _MARKET_TERMS):
        article_assets = _assets(lead_sentence, tickers=tickers)
    elif len(article_assets) == 1 and _ASSET_COMPARISON.search(lead_sentence):
        compared_assets = _assets(lead_sentence, tickers=tickers)
        if len(compared_assets) == 2 and article_assets & compared_assets:
            article_assets.update(compared_assets)
    if not article_assets and not crypto_headline:
        return SubjectAssessment()

    image_assets = _assets(image)
    if article_assets & image_assets or _shared_named_entity(f'{headline} {lead}', image):
        return SubjectAssessment()
    article_subject = _subject_label(article_assets)
    if article_assets and image_assets:
        return SubjectAssessment('asset_mismatch', article_subject, _subject_label(image_assets))

    image_subjects = _literal_subjects(image)
    if image_subjects and not image_subjects & _literal_subjects(f'{headline} {lead}'):
        return SubjectAssessment('unrelated_subject', article_subject, _subject_label(image_subjects))
    return SubjectAssessment()
