"""Small, transparent lexical comparison helpers."""

from __future__ import annotations

import re
from typing import Any

from .identities import normalize_title


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def title_tokens(value: Any) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN_RE.findall(normalize_title(value))
        if token not in _STOPWORDS
    )


def token_set_jaccard(left: Any, right: Any) -> float:
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
