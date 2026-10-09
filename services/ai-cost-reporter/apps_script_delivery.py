'''Deliver bounded PDF/JSON reports to an authenticated Apps Script receiver.'''

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests


PROTOCOL_VERSION = 1
CANONICAL_PREFIX = 'AI-COST-REPORTER-V1'
MAX_PDF_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
TIMESTAMP_WINDOW_SECONDS = 300
FILE_TYPES = (('pdf', 'application/pdf', MAX_PDF_BYTES),
              ('json', 'application/json', MAX_JSON_BYTES))
_HEX_SECRET = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_NONCE = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_TIMESTAMP = re.compile(r'(0|[1-9][0-9]{0,10})\Z', re.ASCII)
_FILE_ID = re.compile(r'[A-Za-z0-9_-]{1,256}\Z', re.ASCII)
_DEPLOYMENT_PATH = re.compile(r'/macros/s/[A-Za-z0-9_-]{1,256}/exec\Z', re.ASCII)


class AppsScriptDeliveryError(RuntimeError):
    '''A configuration, authentication, integrity or delivery failure.'''


def _validate_secret(secret: str) -> None:
    '''Require the agreed 32-byte random secret displayed as lowercase hex.'''
    if not isinstance(secret, str) or _HEX_SECRET.fullmatch(secret) is None:
        raise AppsScriptDeliveryError(
            'GOOGLE_APPS_SCRIPT_SHARED_SECRET must be 64 lowercase hexadecimal characters'
        )


def validate_web_app_url(url: str) -> None:
    '''Allow only a deployed Apps Script HTTPS endpoint, without query parameters.'''
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        raise AppsScriptDeliveryError('GOOGLE_APPS_SCRIPT_WEB_APP_URL is invalid') from None
    if (not isinstance(url, str) or url != url.strip()
            or any(ord(character) <= 32 or ord(character) >= 127 for character in url)
            or parts.scheme != 'https' or parts.netloc != 'script.google.com'
            or _DEPLOYMENT_PATH.fullmatch(parts.path) is None
            or parts.query or parts.fragment):
        raise AppsScriptDeliveryError(
            'GOOGLE_APPS_SCRIPT_WEB_APP_URL must be a deployed Google /exec HTTPS URL'
        )


def canonical_string(envelope: Mapping[str, Any]) -> str:
    '''Join signing fields with LF, PDF before JSON, and no trailing newline.

    The HMAC key is the UTF-8 bytes of the literal 64-character hexadecimal
    secret, not the decoded random bytes. File hashes cover the raw file bytes;
    neither base64 content nor JSON formatting is included in this string.
    '''
    try:
        files = envelope['files']
        if (type(envelope['version']) is not int or envelope['version'] != PROTOCOL_VERSION
                or not isinstance(envelope['timestamp'], str)
                or _TIMESTAMP.fullmatch(envelope['timestamp']) is None
                or not isinstance(envelope['nonce'], str)
                or _NONCE.fullmatch(envelope['nonce']) is None
                or not isinstance(envelope['report_date'], str)
                or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', envelope['report_date']) is None
                or type(envelope['replace']) is not bool
                or not isinstance(files, list) or len(files) != 2):
            raise ValueError('Invalid signing fields')
        date_text = envelope['report_date']
        lines = [CANONICAL_PREFIX, envelope['timestamp'], envelope['nonce'],
                 date_text, '1' if envelope['replace'] else '0']
        for index, (extension, mime_type, maximum) in enumerate(FILE_TYPES):
            item = files[index]
            if (not isinstance(item, dict)
                    or item.get('name') != f'ai-cost-report-{date_text}.{extension}'
                    or item.get('mime_type') != mime_type
                    or type(item.get('size')) is not int or not 0 < item['size'] <= maximum
                    or not isinstance(item.get('sha256'), str)
                    or _HEX_SECRET.fullmatch(item['sha256']) is None):
                raise ValueError('Invalid file signing fields')
            lines.extend((item['name'], item['mime_type'], str(item['size']), item['sha256']))
        return '\n'.join(lines)
    except (KeyError, TypeError, ValueError):
        raise AppsScriptDeliveryError('Apps Script signing fields are invalid') from None


def sign_envelope(envelope: Mapping[str, Any], secret: str) -> str:
    '''Return HMAC-SHA256 over the exact UTF-8 canonical string as lowercase hex.'''
    _validate_secret(secret)
    return hmac.new(secret.encode('utf-8'), canonical_string(envelope).encode('utf-8'),
                    hashlib.sha256).hexdigest()


def _strict_json(content: bytes, purpose: str) -> Any:
    '''Reject non-UTF-8, duplicate object keys, non-finite constants and bad JSON.'''
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        '''Detect ambiguous duplicate keys before interpreting a JSON object.'''
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        '''Reject JSON extensions such as NaN or Infinity.'''
        raise ValueError('Non-finite JSON constant')

    try:
        return json.loads(content.decode('utf-8'), object_pairs_hook=object_pairs,
                          parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise AppsScriptDeliveryError(f'{purpose} contains invalid JSON') from None


def _read_artifact(path: Path, expected_name: str, maximum: int) -> bytes:
    '''Read at most the allowed byte count, including a one-byte overflow check.'''
    if path.name != expected_name:
        raise AppsScriptDeliveryError('Report artifact filename does not match the report date')
    try:
        with path.open('rb') as handle:
            content = handle.read(maximum + 1)
    except OSError:
        raise AppsScriptDeliveryError('Report artifact cannot be read') from None
    if not content or len(content) > maximum:
        raise AppsScriptDeliveryError('Report artifact exceeds its size limit or is empty')
    return content


def build_envelope(
    report_date: date, pdf_path: Path, json_path: Path, secret: str,
    replace: bool = False, *, timestamp: str | None = None, nonce: str | None = None,
) -> dict[str, Any]:
    '''Validate artifacts and create a fresh authenticated upload request.

    Timestamp/nonce overrides exist for offline cross-language protocol vectors.
    Publication always uses the current timestamp and a cryptographically random
    nonce, including when retrying after a response was lost.
    '''
    _validate_secret(secret)
    if (type(report_date) is not date or report_date < date(2000, 1, 1)
            or report_date >= datetime.now(timezone.utc).date()):
        raise AppsScriptDeliveryError('Report date must be a completed UTC day from 2000 onward')
    if type(replace) is not bool:
        raise AppsScriptDeliveryError('Report replacement flag must be a boolean')
    date_text = report_date.isoformat()
    files: list[dict[str, Any]] = []
    for index, (extension, mime_type, maximum) in enumerate(FILE_TYPES):
        filename = f'ai-cost-report-{date_text}.{extension}'
        content = _read_artifact(Path((pdf_path, json_path)[index]), filename, maximum)
        if extension == 'pdf':
            if not content.startswith(b'%PDF-') or b'%%EOF' not in content[-1024:]:
                raise AppsScriptDeliveryError('PDF report artifact has an invalid format')
        else:
            report = _strict_json(content, 'Report artifact')
            if (not isinstance(report, dict)
                    or report.get('data_type') != 'PROVIDER_REPORTED_API_SPEND'
                    or report.get('currency') != 'USD'
                    or report.get('report_date_utc') != date_text
                    or not isinstance(report.get('providers'), dict)
                    or not all(isinstance(report['providers'].get(provider), dict)
                               for provider in ('openai', 'xai'))):
                raise AppsScriptDeliveryError('JSON artifact is not the expected provider billing report')
        files.append({'name': filename, 'mime_type': mime_type, 'size': len(content),
                      'sha256': hashlib.sha256(content).hexdigest(),
                      'content_base64': base64.b64encode(content).decode('ascii')})
    envelope = {'version': PROTOCOL_VERSION,
                'timestamp': str(int(time.time())) if timestamp is None else timestamp,
                'nonce': secrets.token_hex(16) if nonce is None else nonce,
                'report_date': date_text, 'replace': replace, 'files': files}
    envelope['signature'] = sign_envelope(envelope, secret)
    return envelope


class AppsScriptPublisher:
    '''POST private reports and accept only authenticated-protocol success bodies.'''

    def __init__(self, session: requests.Session, url: str, secret: str) -> None:
        '''Use a dedicated session without implicit local credentials or proxies.'''
        validate_web_app_url(url)
        _validate_secret(secret)
        # A fresh delivery session avoids inherited auth/cookies; disabling
        # trust_env prevents requests from reading netrc or proxy credentials.
        session.trust_env = False
        self.session = session
        self.url = url
        self.secret = secret

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        '''Disable automatic redirects and retain TLS certificate verification.'''
        try:
            return self.session.request(method, url, timeout=(10, 60),
                                        allow_redirects=False, stream=True,
                                        verify=True, **kwargs)
        except requests.RequestException:
            raise AppsScriptDeliveryError('Apps Script delivery failed: network error') from None

    @staticmethod
    def _redirect_url(response: requests.Response) -> str:
        '''Permit only the documented ContentService response host as a GET.'''
        destination = response.headers.get('Location', '')
        try:
            parts = urlsplit(destination)
        except (TypeError, ValueError):
            raise AppsScriptDeliveryError('Apps Script delivery returned an unsafe redirect') from None
        if (not isinstance(destination, str) or not 1 <= len(destination) <= 8192
                or any(ord(character) <= 32 or ord(character) >= 127 for character in destination)
                or parts.scheme != 'https' or parts.netloc != 'script.googleusercontent.com'
                or not parts.path.startswith('/') or parts.fragment):
            raise AppsScriptDeliveryError('Apps Script delivery returned an unsafe redirect')
        return destination

    @staticmethod
    def _response_body(response: requests.Response) -> bytes:
        '''Bound streamed response bytes before parsing any success/error JSON.'''
        if response.status_code != 200:
            raise AppsScriptDeliveryError(
                f'Apps Script delivery failed: HTTP {response.status_code}'
            )
        length = response.headers.get('Content-Length')
        if length is not None:
            if not re.fullmatch(r'[0-9]{1,20}', length) or int(length) > MAX_RESPONSE_BYTES:
                raise AppsScriptDeliveryError('Apps Script response exceeds its size limit')
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise AppsScriptDeliveryError('Apps Script response exceeds its size limit')
        except requests.RequestException:
            raise AppsScriptDeliveryError('Apps Script response failed: network error') from None
        return bytes(body)

    @staticmethod
    def _validate_result(body: bytes, envelope: Mapping[str, Any]) -> dict[str, str]:
        '''Require exact report identities/hashes even when HTTP status is 200.'''
        data = _strict_json(body, 'Apps Script response')
        if not isinstance(data, dict):
            raise AppsScriptDeliveryError('Apps Script response is not an object')
        if data.get('ok') is not True:
            code = data.get('error')
            safe_codes = {'invalid_request', 'invalid_signature', 'expired_timestamp',
                          'replayed_request', 'replay_capacity', 'storage_failure',
                          'conflict', 'duplicates', 'busy', 'configuration_error'}
            if isinstance(code, str) and code in safe_codes:
                raise AppsScriptDeliveryError(f'Apps Script delivery rejected: {code}')
            raise AppsScriptDeliveryError('Apps Script response did not confirm successful delivery')
        if (type(data.get('version')) is not int or data['version'] != PROTOCOL_VERSION
                or data.get('report_date') != envelope['report_date']
                or type(data.get('duplicate')) is not bool
                or not isinstance(data.get('files'), dict)
                or set(data['files']) != {'pdf', 'json'}):
            raise AppsScriptDeliveryError('Apps Script success response has an invalid schema')
        result: dict[str, str] = {}
        for index, extension in enumerate(('pdf', 'json')):
            record = data['files'][extension]
            expected = envelope['files'][index]
            if (not isinstance(record, dict) or not isinstance(record.get('id'), str)
                    or _FILE_ID.fullmatch(record['id']) is None
                    or record.get('name') != expected['name']
                    or record.get('sha256') != expected['sha256']):
                raise AppsScriptDeliveryError('Apps Script response does not match the uploaded artifacts')
            result[extension] = record['id']
        if result['pdf'] == result['json']:
            raise AppsScriptDeliveryError('Apps Script response did not identify two distinct report files')
        return result

    def publish(
        self, report_date: date, pdf_path: Path, json_path: Path, replace: bool = False,
    ) -> dict[str, str]:
        '''Publish both reports; a new attempt uses a fresh timestamp and nonce.'''
        envelope = build_envelope(report_date, pdf_path, json_path, self.secret, replace)
        body = json.dumps(envelope, ensure_ascii=True, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')
        if len(body) > MAX_REQUEST_BYTES:
            raise AppsScriptDeliveryError('Apps Script request exceeds its size limit')
        response = self._request('POST', self.url, data=body,
                                 headers={'Content-Type': 'application/json'})
        try:
            if response.status_code in (302, 303):
                destination = self._redirect_url(response)
                response.close()
                response = self._request('GET', destination)
            response_body = self._response_body(response)
        finally:
            response.close()
        return self._validate_result(response_body, envelope)
