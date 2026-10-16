'''Optional metadata-only DEV capture; no request, response, article or URL is saved.

Disabled unless ARTICLE_QUALITY_DEV_CAPTURE_ENABLED=true. The operator must set
ARTICLE_QUALITY_DEV_CAPTURE_DIRECTORY and ARTICLE_QUALITY_DEV_CAPTURE_AUTHORIZATION
to absolute paths outside the checkout and publicly served directories. Capture is
supported only in a POSIX DEV container: Windows fails closed because chmod alone
does not establish a private ACL there.

The owner-only authorization JSON is an independent operator attestation, after
checking DEV destinations. It contains authorize_dev_capture=true, expires_at
(UTC ISO timestamp, at most 24 hours ahead), database_host, database_name,
wordpress_origin, approved_raw_ids (at most 10), and optional max_events (<=200),
max_total_bytes (<=5242880), max_event_bytes (<=65536). APP_ENV is not consulted.
It authorizes only exact configured destination matches, not arbitrary URLs.
The manifest must also pin private_output_directory and attest
output_not_public=true after independently checking web-server mappings.

Only fixed identifiers, counts, hashes, boolean checks and symbolic codes are saved.
Bodies are measured in memory. Requests and unknown fields are never traversed.
Artifacts expire with the authorization. The operator must remove the private
run directory after review and no later than expires_at; this helper does not run
background cleanup or retain application state. Never upload artifacts to Git.
'''

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
from threading import Lock
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid


ENABLE_ENV = 'ARTICLE_QUALITY_DEV_CAPTURE_ENABLED'
DIRECTORY_ENV = 'ARTICLE_QUALITY_DEV_CAPTURE_DIRECTORY'
AUTHORIZATION_ENV = 'ARTICLE_QUALITY_DEV_CAPTURE_AUTHORIZATION'
def _application_root(module_path: Path) -> Path:
    code_directory = module_path.parent
    return code_directory.parent if code_directory.name.lower() == 'getnewsapi' else code_directory


_REPOSITORY = _application_root(Path(__file__).resolve())
_PUBLIC_COMPONENTS = {'public', 'public_html', 'www', 'wwwroot', 'htdocs', 'static', 'wp-content', 'uploads'}
_STAGES = frozenset(
    f'{phase}.{boundary}' for phase in ('rewrite', 'expansion', 'repair')
    for boundary in ('request', 'response', 'pre_parse', 'parsed', 'validation_failed', 'parsing_failed')
) | {
    'repair.checks', 'repair.selected_before_cleanup', 'repair.post_cleanup',
    'article.post_cleanup', 'article.before_storage',
}
_PROVIDERS = frozenset({'grok', 'openai', 'unknown'})
_MODELS = frozenset({'grok_text', 'openai_text', 'unknown'})
_FINISH_REASONS = frozenset({'stop', 'length', 'content_filter', 'tool_calls', 'function_call', 'unknown'})
_VALIDATION_STATUSES = frozenset({'passed', 'failed', 'not_available'})
_VALIDATION_CODES = frozenset({
    'missing_title', 'missing_full_text', 'missing_category', 'missing_hashtags',
    'missing_sentiment', 'missing_seo_focus', 'missing_seo_slug', 'missing_seo_meta',
    'missing_image_alt', 'empty_full_text', 'missing_paragraph',
    'body_under_450_words', 'empty_title', 'empty_seo_slug', 'empty_seo_meta',
})
_SOFT_CHECKS = frozenset({
    'len_ok', 'h2_ok', 'sent_len_ok', 'transitions_ok', 'kw_in_intro',
    'kw_in_title', 'kw_in_slug', 'kw_in_meta', 'kw_in_h2', 'kw_in_alt',
    'kw_density_ok', 'meta_len_ok', 'title_len_ok', 'at_least_one_external',
})
_REPAIR_TRIGGERS = frozenset({'h2_ok', 'at_least_one_external'})
_MAX_ID = (1 << 63) - 1
_POSIX_PRIVATE_FILES = os.name == 'posix'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _origin(value: object) -> str:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return ''
        port = parsed.port
        suffix = '' if port in {None, 443 if parsed.scheme == 'https' else 80} else f':{port}'
        host = parsed.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        return parsed.scheme + '://' + host + suffix
    except (TypeError, ValueError):
        return ''


def _private_path(path: Path, *, directory: bool) -> bool:
    '''Fail closed when owner-only POSIX permissions cannot be proven.'''
    if not _POSIX_PRIVATE_FILES:
        return False
    try:
        info = path.lstat()
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        return bool(correct_type and info.st_uid == os.getuid() and not info.st_mode & 0o077)
    except (OSError, AttributeError):
        return False


def _safe_location(path: Path) -> bool:
    if not path.is_absolute() or any(part.lower() in _PUBLIC_COMPONENTS for part in path.parts):
        return False
    try:
        if path.is_relative_to(_REPOSITORY):
            return False
        for ancestor in (path, *path.parents):
            if ancestor.is_symlink():
                return False
            if ancestor.exists() and _POSIX_PRIVATE_FILES:
                mode = ancestor.stat().st_mode
                # /tmp is acceptable only with the sticky bit; the output itself
                # is separately required to be owner-only.
                if mode & 0o022 and not mode & stat.S_ISVTX:
                    return False
        return path.resolve() == path
    except (OSError, RuntimeError):
        return False


def _bounded_integer(value: object, default: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('invalid capture limit')
    return value


def configure_capture(
    *, database_host: str | None, database_name: str | None,
    wordpress_api_url: str | None, secret_values: tuple[str, ...] = (),
    environment: Mapping[str, str] | None = None,
) -> CaptureRecorder | None:
    '''Return an authorized bounded recorder, or None; never affect generation.

    secret_values is accepted for call compatibility and is never read or retained.
    '''
    try:
        env = os.environ if environment is None else environment
        enabled = env.get(ENABLE_ENV, '')
        if type(enabled) is not str or enabled.lower() != 'true' or not _POSIX_PRIVATE_FILES:
            return None
        output = Path(env.get(DIRECTORY_ENV, ''))
        authorization = Path(env.get(AUTHORIZATION_ENV, ''))
        if not _safe_location(output) or not _safe_location(authorization):
            return None
        if not _private_path(authorization, directory=False) or authorization.stat().st_size > 16384:
            return None
        with authorization.open(encoding='utf-8') as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict) or manifest.get('authorize_dev_capture') is not True:
            return None
        if manifest.get('private_output_directory') != str(output) or manifest.get('output_not_public') is not True:
            return None
        now = _utc_now()
        expiry_value = manifest.get('expires_at')
        if type(expiry_value) is not str:
            return None
        expires = datetime.fromisoformat(expiry_value.replace('Z', '+00:00'))
        if expires.tzinfo is None or not now < expires <= now + timedelta(hours=24):
            return None
        origin = _origin(wordpress_api_url)
        if not database_host or not database_name or not origin:
            return None
        if (manifest.get('database_host') != database_host
                or manifest.get('database_name') != database_name
                or manifest.get('wordpress_origin') != origin):
            return None
        ids = manifest.get('approved_raw_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(type(i) is not int or not 1 <= i <= _MAX_ID for i in ids):
            return None
        limits = (
            _bounded_integer(manifest.get('max_events'), 100, 200),
            _bounded_integer(manifest.get('max_total_bytes'), 2097152, 5242880),
            _bounded_integer(manifest.get('max_event_bytes'), 65536, 65536),
        )
        output.mkdir(mode=0o700, parents=False, exist_ok=True)
        if not _private_path(output, directory=True):
            return None
        run = output / ('capture-' + now.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex)
        run.mkdir(mode=0o700)
        if not _private_path(run, directory=True):
            return None
        return CaptureRecorder(run, frozenset(ids), expires, limits)
    except Exception:
        return None


def _controlled(value: object, allowed: frozenset[str]) -> str:
    return value if type(value) is str and value in allowed else 'unknown'


def _symbolic_codes(values: object, allowed: frozenset[str]) -> list[str]:
    if type(values) not in {list, tuple}:
        return []
    return sorted({value for value in values[:64] if type(value) is str and value in allowed})


def _article_metrics(document: object, raw_text: object) -> dict[str, Any]:
    '''Inspect only full_text in memory; never return article or response strings.'''
    parse_status = 'not_available'
    candidate = None
    if type(document) is dict:
        candidate = document
    elif document is not None:
        parse_status = 'non_object'
    elif type(raw_text) is str:
        try:
            decoded = json.loads(raw_text)
            if type(decoded) is dict:
                candidate = decoded
            else:
                parse_status = 'non_object'
        except (ValueError, TypeError, RecursionError):
            parse_status = 'invalid_json'
    body = candidate.get('full_text') if candidate is not None else None
    result: dict[str, Any] = {
        'parse_status': parse_status,
        'article_word_count': None,
        'punctuation': None,
        'content_sha256': None,
        'content_hash_scope': None,
    }
    if type(body) is not str:
        if candidate is not None:
            result['parse_status'] = 'missing_body'
        return result
    without_tags = re.sub(r'<[^>]+>', ' ', body)
    visible_body = html.unescape(without_tags)
    result.update({
        'parse_status': 'parsed',
        'article_word_count': len(re.findall(r'\b\w+\b', without_tags)),
        'punctuation': {name: visible_body.count(character) for name, character in (
            ('comma', ','), ('period', '.'), ('semicolon', ';'), ('colon', ':'),
            ('question', '?'), ('exclamation', '!'),
        )},
        'content_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'content_hash_scope': 'article_html',
    })
    return result


class CaptureRecorder:
    '''One run's in-memory budget; output contains only fixed diagnostic metadata.'''

    def __init__(self, directory: Path, approved_ids: frozenset[int], expires: datetime,
                 limits: tuple[int, int, int], secret_values: tuple[str, ...] = ()) -> None:
        # secret_values is deliberately ignored for backwards call compatibility.
        self.directory = directory
        self.approved_ids = approved_ids
        self.expires = expires
        self.max_events, self.max_total_bytes, self.max_event_bytes = limits
        self._events = 0
        self._bytes = 0
        self._lock = Lock()
        self._suppressed_ids: set[int] = set()

    def record_stage(
        self, event: str, *, article_context: dict | None, provider: str | None,
        model: str | None, attempt: int | None = None,
        request_messages: object = None, raw_text: object = None,
        document: object = None, validation_failures: object = None,
        checks: object = None, finish_reason: object = None,
        suppress_evidence: bool = False, validation_status: object = None,
        repair_triggers: object = None, **_ignored: object,
    ) -> bool:
        '''Inspect bodies solely for metrics/hash; never inspect or persist requests.

        Unknown keywords and input fields are ignored, never walked or converted.
        Every output string is a fixed identifier, timestamp, or SHA-256 digest.
        '''
        try:
            with self._lock:
                if type(event) is not str or event not in _STAGES:
                    return False
                article_id = article_context.get('id') if type(article_context) is dict else None
                if type(article_id) is not int or article_id not in self.approved_ids:
                    return False
                if _utc_now() >= self.expires or self._events >= self.max_events:
                    return False
                if suppress_evidence is True:
                    self._suppressed_ids.add(article_id)
                suppressed = article_id in self._suppressed_ids
                metrics = _article_metrics(None, None) if suppressed else _article_metrics(document, raw_text)
                if suppressed:
                    metrics['parse_status'] = 'suppressed'
                fixed_checks: dict[str, bool] = {}
                selected_triggers = _symbolic_codes(repair_triggers, _REPAIR_TRIGGERS)
                if type(checks) is dict:
                    selected_triggers = sorted(set(selected_triggers) | set(
                        _symbolic_codes(checks.get('selected_repair_triggers'), _REPAIR_TRIGGERS)
                    ))
                    candidate_checks = checks
                    status = checks.get('status')
                    if type(status) is dict and type(status.get('checks')) is dict:
                        candidate_checks = status['checks']
                    for name in sorted(_SOFT_CHECKS):
                        value = candidate_checks.get(name)
                        if type(value) is bool:
                            fixed_checks[name] = value
                status_code = validation_status if (
                    type(validation_status) is str and validation_status in _VALIDATION_STATUSES
                ) else 'not_available'
                payload: dict[str, Any] = {
                    'schema_version': 2,
                    'raw_article_id': article_id,
                    'stage': event,
                    'provider': _controlled(provider, _PROVIDERS),
                    'model': _controlled(model, _MODELS),
                    'attempt': attempt if type(attempt) is int and 1 <= attempt <= 1000 else None,
                    'timestamp_utc': _utc_now().isoformat(),
                    'expires_at': self.expires.isoformat(),
                    'response_character_count': len(raw_text) if type(raw_text) is str else None,
                    'finish_reason': _controlled(finish_reason, _FINISH_REASONS) if finish_reason is not None else None,
                    'validation_status': status_code,
                    'validation_failures': _symbolic_codes(validation_failures, _VALIDATION_CODES),
                    'repair_triggers': selected_triggers,
                    'checks': fixed_checks,
                    **metrics,
                }
                if suppressed:
                    payload['evidence_incomplete_unexpected_provider_content'] = True
                encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8')
                if len(encoded) > self.max_event_bytes or self._bytes + len(encoded) > self.max_total_bytes:
                    return False
                if not _private_path(self.directory, directory=True):
                    return False
                name = self.directory / (f'{self._events + 1:04d}-' + uuid.uuid4().hex + '.json')
                descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(descriptor, 'wb') as handle:
                        handle.write(encoded)
                except Exception:
                    name.unlink(missing_ok=True)
                    raise
                if not _private_path(name, directory=False):
                    name.unlink(missing_ok=True)
                    return False
                self._events += 1
                self._bytes += len(encoded)
                return True
        except Exception:
            return False


def record_stage(event: str, *, recorder: CaptureRecorder | None = None, **kwargs: Any) -> bool:
    '''Convenient no-op entry point; the caller owns a single recorder per run.'''
    try:
        return bool(recorder is not None and recorder.record_stage(event, **kwargs))
    except Exception:
        return False
