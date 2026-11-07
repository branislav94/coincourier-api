"""Pure, bounded observations of workflow phrases in public article HTML.

Matches are review candidates, never an editorial verdict or a publishing gate.
This module imports no application configuration, providers or repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import logging
import re


MAX_HTML_CHARACTERS = 100_000
MAX_FINDINGS = 64
PHRASES = (
    ("generation_time", "generation time"),
    ("reporting_cutoff", "reporting cutoff"),
    ("supplied_evidence", "supplied evidence"),
    ("available_evidence", "available evidence"),
    ("reviewed_coverage", "reviewed coverage"),
)
KINDS = ("workflow_reference_candidate", "quoted_candidate", "ambiguous_candidate")
_PATTERNS = tuple(
    (rule, re.compile(r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)", re.I))
    for rule, phrase in PHRASES
)
_WORKFLOW_CONTEXT = re.compile(
    r"\b(?:this\s+(?:prompt|draft)|"
    r"(?:provided|supplied)\s+to\s+(?:this|the)\s+(?:model|writer)|"
    r"(?:the|this)\s+generated\s+(?:article|response))\b",
    re.I,
)
_CLAUSE_BOUNDARY = re.compile(r"[.!?;:\n]")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicCopyFinding:
    rule: str
    kind: str


@dataclass(frozen=True)
class PublicCopyAssessment:
    status: str
    findings: tuple[PublicCopyFinding, ...] = ()
    truncated: bool = False


class _VisibleBlocks(HTMLParser):
    """Keep inline continuity, block boundaries and explicit HTML quotations."""

    _BLOCKS = frozenset({
        "p", "div", "section", "article", "li", "ul", "ol", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr", "td", "th",
    })
    _IGNORED = frozenset({"script", "style", "noscript", "template", "pre", "code"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, tuple[bool, ...]]] = []
        self._chunks: list[str] = []
        self._quoted: list[bool] = []
        self._ignored: list[str] = []
        self._quotes: list[str] = []

    def flush(self) -> None:
        if self._chunks:
            self.blocks.append(("".join(self._chunks), tuple(self._quoted)))
            self._chunks = []
            self._quoted = []

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag in self._IGNORED:
            self.flush()
            self._ignored.append(tag)
            return
        if self._ignored:
            return
        if tag in self._BLOCKS:
            self.flush()
        if tag in {"q", "blockquote"}:
            self._quotes.append(tag)
        if tag == "br":
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._ignored:
            self._close_tag(self._ignored, tag)
            return
        if tag in self._BLOCKS:
            self.flush()
        self._close_tag(self._quotes, tag)

    @staticmethod
    def _close_tag(stack: list[str], tag: str) -> None:
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] == tag:
                del stack[index:]
                return

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self._chunks.append(data)
            self._quoted.extend([bool(self._quotes)] * len(data))


def _quotation_masks(text: str, html_quoted: tuple[bool, ...]) -> tuple[list[bool], list[bool]]:
    """Protect balanced quotations; abstain after an unmatched opening quote.

    Apostrophes inside words are not quotation delimiters. This recognizes
    presentation, not the authenticity or attribution of any quotation.
    """
    quoted = list(html_quoted)
    uncertain = [False] * len(text)
    opening: int | None = None
    closing = ""
    for index, character in enumerate(text):
        between_word_characters = (
            index > 0 and index + 1 < len(text)
            and text[index - 1].isalnum() and text[index + 1].isalnum()
        )
        if character in {"'", "’"} and between_word_characters:
            continue
        if opening is None:
            if character in {'"', "“", "‘"} or (
                character == "'" and (index == 0 or not text[index - 1].isalnum())
            ):
                opening = index
                closing = {"“": "”", "‘": "’"}.get(character, character)
        elif character == closing:
            quoted[opening:index + 1] = [True] * (index + 1 - opening)
            opening = None
    if opening is not None:
        uncertain[opening:] = [True] * (len(text) - opening)
    return quoted, uncertain


def _has_workflow_context(text: str, start: int, end: int) -> bool:
    left = max(0, start - 160)
    right = min(len(text), end + 160)
    for boundary in _CLAUSE_BOUNDARY.finditer(text, left, start):
        left = boundary.end()
    boundary = _CLAUSE_BOUNDARY.search(text, end, right)
    if boundary is not None:
        right = boundary.start()
    return _WORKFLOW_CONTEXT.search(text, left, right) is not None


def assess_public_copy(html: object) -> PublicCopyAssessment:
    """Observe five literal phrases without altering input or raising a gate.

    Only article-body text is in scope. Attributes, scripts, comments, code and
    preformatted examples are excluded. Ordinary reporting and quoted uses
    remain candidates; no match means neither factual nor editorial approval.
    """
    if type(html) is not str:
        return PublicCopyAssessment("not_evaluated_invalid_input")
    if len(html) > MAX_HTML_CHARACTERS:
        return PublicCopyAssessment("not_evaluated_oversize")
    parser = _VisibleBlocks()
    try:
        parser.feed(html)
        parser.close()
        parser.flush()
    except Exception:
        return PublicCopyAssessment("not_evaluated_parse_error")
    findings: list[PublicCopyFinding] = []
    for text, html_quoted in parser.blocks:
        quoted, uncertain = _quotation_masks(text, html_quoted)
        for rule, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                if len(findings) == MAX_FINDINGS:
                    return PublicCopyAssessment("evaluated", tuple(findings), truncated=True)
                start, end = match.span()
                if any(quoted[start:end]):
                    kind = "quoted_candidate"
                elif any(uncertain[start:end]):
                    kind = "ambiguous_candidate"
                elif _has_workflow_context(text, start, end):
                    kind = "workflow_reference_candidate"
                else:
                    kind = "ambiguous_candidate"
                findings.append(PublicCopyFinding(rule, kind))
    return PublicCopyAssessment("evaluated", tuple(findings))


def _safe_id(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def observe_public_copy(
    html: object, *, mode: str, stage: str,
    raw_article_id: object = None, rich_article_id: object = None,
    logger=None,
) -> PublicCopyAssessment | None:
    """Fail-open shadow adapter; emit controlled metadata, never article text."""
    if mode != "shadow":
        return None
    safe_stage = stage if stage in {"pre_storage", "pre_create"} else "unknown"
    target_logger = logger if logger is not None else _LOGGER
    try:
        assessment = assess_public_copy(html)
        counts = ",".join(
            f"{rule}:{kind}={sum(item.rule == rule and item.kind == kind for item in assessment.findings)}"
            for rule, _phrase in PHRASES for kind in KINDS
        )
        target_logger.info(
            "[PUBLIC-COPY-GATE] mode=shadow stage=%s status=%s raw_article_id=%s "
            "rich_article_id=%s findings=%s truncated=%s counts=%s decision=continue",
            safe_stage, assessment.status, _safe_id(raw_article_id), _safe_id(rich_article_id),
            len(assessment.findings), assessment.truncated, counts,
        )
        return assessment
    except Exception:
        try:
            target_logger.warning(
                "[PUBLIC-COPY-GATE] mode=shadow stage=%s status=observer_unavailable decision=continue",
                safe_stage,
            )
        except Exception:
            pass
        return None
