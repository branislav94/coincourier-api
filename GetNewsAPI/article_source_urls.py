'''Small URL helpers shared by source permissions and generated article cleanup.'''

from __future__ import annotations

from html import escape, unescape
from html.parser import HTMLParser
import ipaddress
import re
import unicodedata
from urllib.parse import unquote_plus, urlsplit


_TRACKING_KEYS = {'ref', 'fbclid', 'gclid', 'mc_cid', 'mc_eid'}
_SENSITIVE_KEYS = {
    'access_token', 'api_key', 'apikey', 'auth', 'authorization', 'awsaccesskeyid',
    'code', 'credential', 'expires', 'expiry', 'exp', 'googleaccessid', 'hash',
    'hmac', 'key', 'key-pair-id', 'nonce', 'policy', 'sig', 'signature', 'state',
    'token', 'jwt', 'hdnea', 'hdnts', '__token__',
}
_ATTRIBUTE = re.compile(
    r'''(?P<name>[^\s=/>]+)(?:\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<unquoted>[^\s>]+)))?'''
)
_INVALID_PERCENT = re.compile(r'%(?![0-9a-fA-F]{2})')


def _safe_hostname(hostname: str) -> bool:
    if not hostname or '%' in hostname:
        return False
    hostname = hostname.rstrip('.').lower()
    if hostname == 'wikipedia.org' or hostname.endswith('.wikipedia.org'):
        return False
    if ':' in hostname:
        try:
            ipaddress.IPv6Address(hostname)
            return True
        except ValueError:
            return False
    try:
        ascii_hostname = hostname.encode('idna').decode('ascii')
    except UnicodeError:
        return False
    return len(ascii_hostname) <= 253 and all(
        len(label) <= 63
        and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label)
        for label in ascii_hostname.split('.')
    )


def normalize_source_url(value: object) -> str | None:
    '''Validate HTTP(S), then remove only recognized tracking and fragments.

    Meaningful query segments retain their original bytes, order, duplicates,
    blanks and encoding. Signed/authenticated URLs retain their entire raw
    value, including tracking and fragments: guessing which bytes a signature
    covers would risk breaking the destination. No network lookup is performed.

    Inputs are raw URLs, not HTML attribute values. Decode HTML entities exactly
    once at the attribute boundary, before calling this idempotent normalizer.
    '''
    if not isinstance(value, str) or not value:
        return None
    url = value
    if any(character.isspace() or unicodedata.category(character) in {'Cc', 'Cf', 'Cs'} for character in url):
        return None
    if any(character in url for character in '\\<>"`') or _INVALID_PERCENT.search(url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {'http', 'https'} or not parts.netloc:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        if not _safe_hostname(parts.hostname or ''):
            return None
        authority_pattern = r'\[[^\]]+\](?::[0-9]+)?' if ':' in parts.hostname else r'[^:]+(?::[0-9]+)?'
        if re.fullmatch(authority_pattern, parts.netloc) is None:
            return None
        # Accessing port validates its syntax and range without contacting it.
        parts.port
    except (TypeError, ValueError, UnicodeError):
        return None

    segments = parts.query.split('&')
    keys = [unquote_plus(segment.partition('=')[0]).lower() for segment in segments]
    if any(
        key in _SENSITIVE_KEYS or key.startswith(('x-amz-', 'x-goog-'))
        for key in keys
    ):
        return url

    without_fragment = url.split('#', 1)[0]
    base, query_marker, query = without_fragment.partition('?')
    if not query_marker or not query:
        return without_fragment
    kept = [
        segment for segment, key in zip(segments, keys)
        if not (key.startswith('utm_') or key in _TRACKING_KEYS)
    ]
    return base + ('?' + '&'.join(kept) if kept else '')


def _quoted_href(tag: str) -> tuple[int, int, str, str] | None:
    '''Return only the first real quoted href, ignoring data-* and other values.'''
    for attribute in _ATTRIBUTE.finditer(tag, 2, len(tag) - 1):
        if attribute.group('name').lower() != 'href':
            continue
        for group, quote in (('double', '"'), ('single', "'")):
            if attribute.group(group) is not None:
                return attribute.start(group), attribute.end(group), attribute.group(group), quote
        return None
    return None


def _anchor_spans(text_html: str) -> list[tuple[int, str]]:
    '''Locate real tags without serializing HTML or touching comments/scripts.'''
    line_offsets = [0] + [match.end() for match in re.finditer('\n', text_html)]
    anchors: list[tuple[int, str]] = []

    class AnchorParser(HTMLParser):
        def handle_starttag(self, tag: str, _attrs) -> None:
            if tag.lower() == 'a':
                line, column = self.getpos()
                anchors.append((line_offsets[line - 1] + column, self.get_starttag_text()))

        def handle_startendtag(self, tag: str, attrs) -> None:
            self.handle_starttag(tag, attrs)

    parser = AnchorParser(convert_charrefs=False)
    parser.feed(text_html)
    parser.close()
    return anchors


def extract_source_urls(text_html: str) -> list[str]:
    '''Extract distinct normalized URLs from single/double-quoted anchor hrefs.'''
    seen: set[str] = set()
    result: list[str] = []
    for _offset, anchor in _anchor_spans(text_html or ''):
        href = _quoted_href(anchor)
        normalized = normalize_source_url(unescape(href[2])) if href else None
        if normalized is not None and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def clean_approved_article_links(text_html: str, allowed_sources: list[str]) -> str:
    '''Clean approved anchor targets only, preserving other HTML byte-for-byte.

    This is cleanup, not a new permission source or an outbound-link validator.
    Unapproved links, internal targets and video elements are left unchanged.
    '''
    approved = {
        normalized for source in allowed_sources
        if (normalized := normalize_source_url(source)) is not None
    }
    if not approved:
        return text_html

    replacements: list[tuple[int, int, str]] = []
    for offset, tag in _anchor_spans(text_html):
        href = _quoted_href(tag)
        if href is None:
            continue
        start, end, value, quote = href
        decoded_value = unescape(value)
        normalized = normalize_source_url(decoded_value)
        if normalized not in approved or normalized == decoded_value:
            continue
        replacement = escape(normalized, quote=False)
        replacement = replacement.replace(quote, '&quot;' if quote == '"' else '&#x27;')
        replacements.append((offset + start, offset + end, replacement))

    for start, end, replacement in reversed(replacements):
        text_html = text_html[:start] + replacement + text_html[end:]
    return text_html
