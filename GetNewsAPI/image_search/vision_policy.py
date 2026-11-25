"""Conservative article and image subject checks for the Vision pilot."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .subject import subject_article_headline, subject_article_lead


_HARDWARE_TOPIC = re.compile(
    r'\b(?:coldcard|hardware wallets?|recovery seeds?|seed phrases?|wallet seeds?)\b'
)
_FINANCIAL_TOPIC = re.compile(
    r'\b(?:etfs?|exchange traded funds?|fund (?:flows?|inflows?|outflows?)|'
    r'market cap(?:italization)?|financial (?:records?|markets?)|'
    r'(?:bitcoin|crypto|stock|share|bond|market) (?:prices?|funds?|inflows?|outflows?)|'
    r'earnings|trading volumes?|assets under management)\b'
)
_RECORD = re.compile(r'\brecords?\b')
_BOTANICAL_TOPIC = re.compile(
    r'\b(?:dandelions?|botan(?:y|ical)|wildflowers?|flowers?|plants? seeds?|'
    r'seed dispersal|pollen|germinat(?:e|ion)|seedlings?)\b'
)
_VINYL_TOPIC = re.compile(
    r'\b(?:vinyl|record players?|turntables?|phonographs?|albums?|record labels?)\b'
)

_BOTANICAL_LABELS = frozenset({
    'dandelion', 'dandelion seed', 'dandelion seeds', 'wildflower', 'flower bouquet',
    'cut flowers', 'flower petal', 'flower petals',
})
_HARDWARE_LABELS = frozenset({
    'usb', 'usb device', 'usb flash drive', 'usb drive', 'flash drive', 'usb hardware',
    'computer hardware', 'electronic device', 'electronics', 'electronic component',
    'printed circuit board', 'hardware wallet', 'metal seed backup plate', 'seed backup plate',
})
_VINYL_LABELS = frozenset({'vinyl record', 'vinyl records', 'record player', 'turntable', 'phonograph'})
_FINANCIAL_LABELS = frozenset({
    'market chart', 'stock chart', 'stock market', 'financial chart', 'financial document',
    'financial statement', 'financial report', 'line chart', 'bar chart', 'candlestick chart',
    'chart', 'graph', 'spreadsheet',
})


def _words(text: str) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', text.casefold()))


def classify_vision_topic(article: dict[str, Any]) -> str | None:
    """Identify a supported context using the visible headline and lead."""
    headline = _words(subject_article_headline(article))
    lead = _words(subject_article_lead(article))
    text = f'{headline} {lead}'
    hardware = bool(_HARDWARE_TOPIC.search(text))
    financial_record = any(_FINANCIAL_TOPIC.search(part) and _RECORD.search(part) for part in (headline, lead))
    if hardware and (_BOTANICAL_TOPIC.search(text) or financial_record):
        return 'mixed_topic'
    if financial_record and _VINYL_TOPIC.search(text):
        return 'mixed_topic'
    if hardware:
        return 'hardware_wallet'
    if financial_record:
        return 'financial_record'
    return None


def build_vision_conflict_rule(topic: str) -> Callable[[tuple[tuple[str, float], ...]], str | None]:
    """Return a conflict only for a specific label without plausible counterevidence."""
    conflicting, plausible, reason = {
        'hardware_wallet': (_BOTANICAL_LABELS, _HARDWARE_LABELS, 'botanical_subject'),
        'financial_record': (_VINYL_LABELS, _FINANCIAL_LABELS, 'vinyl_record_subject'),
    }[topic]

    def evaluate(labels: tuple[tuple[str, float], ...]) -> str | None:
        normalized = tuple((_words(label), score) for label, score in labels)
        if any(label in plausible and score >= 0.70 for label, score in normalized):
            return None
        if any(label in conflicting and score >= 0.90 for label, score in normalized):
            return reason
        return None

    return evaluate
