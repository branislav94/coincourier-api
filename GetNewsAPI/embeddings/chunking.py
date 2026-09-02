"""Deterministic visible-text normalization and event-sized chunking."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

from .models import CHUNKER_VERSION, PreparedDocument, TextChunk


CHUNK_TARGET_TOKENS = 500
CHUNK_MIN_TOKENS = 350
CHUNK_MAX_TOKENS = 600
CHUNK_OVERLAP_TOKENS = 0

_TOKEN_RE = re.compile(r"\w+(?:'\w+)*|[^\w\s]", re.UNICODE)
_SENTENCE_BREAK_RE = re.compile(
    r"(?<=[.!?])\s+(?=(?:[\"'(\[])?[A-Z0-9])"
)
_HTML_TAG_RE = re.compile(r"<[A-Za-z][^>]*>")
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "ol",
    "p",
    "section",
    "table",
    "tr",
    "ul",
}


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, _attrs) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and normalized in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and normalized in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


@dataclass(frozen=True)
class _Segment:
    text: str
    starts_paragraph: bool


def estimate_tokens(text: str) -> int:
    """Return a deterministic word/punctuation token estimate."""
    return len(_TOKEN_RE.findall(text))


def _visible_text(value: str) -> str:
    if not _HTML_TAG_RE.search(value):
        return value
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", _visible_text(str(value or "")))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n")]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def normalize_document_text(title: str | None, body: str | None) -> str:
    normalized_title = normalize_text(title)
    normalized_body = normalize_text(body)
    return "\n\n".join(
        part for part in (normalized_title, normalized_body) if part
    )


def _split_oversized(text: str, *, starts_paragraph: bool) -> list[_Segment]:
    words = text.split()
    if not words:
        return []
    pieces: list[_Segment] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and estimate_tokens(candidate) > CHUNK_TARGET_TOKENS:
            pieces.append(
                _Segment(" ".join(current), starts_paragraph and not pieces)
            )
            current = [word]
        else:
            current.append(word)
    if current:
        pieces.append(_Segment(" ".join(current), starts_paragraph and not pieces))
    return pieces


def _segments(text: str) -> list[_Segment]:
    result: list[_Segment] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences = [part.strip() for part in _SENTENCE_BREAK_RE.split(paragraph)]
        first = True
        for sentence in sentences:
            if not sentence:
                continue
            if estimate_tokens(sentence) > CHUNK_MAX_TOKENS:
                pieces = _split_oversized(sentence, starts_paragraph=first)
                result.extend(pieces)
            else:
                result.append(_Segment(sentence, starts_paragraph=first))
            first = False
    return result


def chunk_text(text: str) -> tuple[TextChunk, ...]:
    normalized = normalize_text(text)
    if not normalized:
        return ()

    packed: list[str] = []
    current = ""
    for segment in _segments(normalized):
        separator = "\n\n" if segment.starts_paragraph else " "
        candidate = segment.text if not current else f"{current}{separator}{segment.text}"
        candidate_tokens = estimate_tokens(candidate)
        current_tokens = estimate_tokens(current)
        should_flush = bool(current) and (
            candidate_tokens > CHUNK_MAX_TOKENS
            or (
                candidate_tokens > CHUNK_TARGET_TOKENS
                and current_tokens >= CHUNK_MIN_TOKENS
            )
        )
        if should_flush:
            packed.append(current)
            current = segment.text
        else:
            current = candidate
    if current:
        packed.append(current)

    chunks: list[TextChunk] = []
    seen_hashes: set[str] = set()
    for text_value in packed:
        digest = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        chunks.append(
            TextChunk(
                index=len(chunks),
                text=text_value,
                sha256=digest,
                token_count=estimate_tokens(text_value),
            )
        )
    return tuple(chunks)


def prepare_document(
    title: str | None,
    body: str | None,
    *,
    chunker_version: str = CHUNKER_VERSION,
) -> PreparedDocument:
    if chunker_version != CHUNKER_VERSION:
        raise ValueError(f"unsupported chunker version: {chunker_version}")
    text = normalize_document_text(title, body)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PreparedDocument(
        text=text,
        content_hash=content_hash,
        content_version=f"{chunker_version}:{content_hash}",
        chunker_version=chunker_version,
        chunks=chunk_text(text),
    )
