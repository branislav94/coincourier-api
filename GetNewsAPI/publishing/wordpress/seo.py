'''Synchronize registered Yoast fields through authenticated WordPress REST.'''

from __future__ import annotations

from typing import Any
import logging

import requests


logger = logging.getLogger(__name__)


class MetadataSyncError(ValueError):
    '''Describe an incomplete metadata handoff without exposing article data.'''

    def __init__(self, code: str) -> None:
        '''Retain only a controlled diagnostic code.'''
        self.code = code
        super().__init__(code)


def get_wp_prefix(conn: Any) -> str:
    '''Resolve the existing prefix used by CoinCourier identity persistence.'''
    cur = conn.cursor()
    try:
        cur.execute('''
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name LIKE '%options'
            LIMIT 1
        ''')
        row = cur.fetchone()
        return row[0].replace('options', '') if row else 'wp_'
    finally:
        cur.close()


def validate_yoast_metadata(
    *,
    focus_keyword: str,
    description: str,
    title: str,
    canonical_url: str | None = None,
) -> dict[str, str]:
    '''Require the three supported values without clearing or inventing defaults.'''
    fields = {
        '_yoast_wpseo_focuskw': focus_keyword,
        '_yoast_wpseo_title': title,
        '_yoast_wpseo_metadesc': description,
    }
    for key, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise MetadataSyncError(f'metadata_missing_value:{key}')
    return fields


def _verify_response(
    response: Any,
    post_id: int,
    expected: dict[str, str] | None,
    stage: str,
) -> None:
    '''Reject failed transport, incorrect identity and unverifiable field values.'''
    if response.status_code != 200:
        raise MetadataSyncError(f'metadata_{stage}_http_status:{response.status_code}')
    try:
        payload = response.json()
    except ValueError as exc:
        raise MetadataSyncError(f'metadata_{stage}_invalid_json') from exc
    if not isinstance(payload, dict):
        raise MetadataSyncError(f'metadata_{stage}_invalid_payload')
    if type(payload.get('id')) is not int or payload['id'] != post_id:
        raise MetadataSyncError(f'metadata_{stage}_post_id_mismatch')
    if expected is None:
        return
    metadata = payload.get('meta')
    if not isinstance(metadata, dict):
        raise MetadataSyncError(f'metadata_{stage}_missing_meta')
    for key, value in expected.items():
        if key not in metadata:
            raise MetadataSyncError(f'metadata_{stage}_missing_field:{key}')
        if not isinstance(metadata[key], str) or metadata[key] != value:
            raise MetadataSyncError(f'metadata_{stage}_value_mismatch:{key}')


def write_yoast_metadata(
    http_session: Any,
    post_id: int,
    *,
    focus_keyword: str,
    description: str,
    title: str,
    canonical_url: str | None = None,
    api_base: str,
    timeout: tuple[float, float],
) -> None:
    '''Update three registered fields and verify storage; leave canonical unchanged.'''
    expected = validate_yoast_metadata(
        focus_keyword=focus_keyword,
        description=description,
        title=title,
        canonical_url=canonical_url,
    )
    if type(post_id) is not int or post_id <= 0:
        raise MetadataSyncError('metadata_invalid_post_id')
    site_url = api_base.rstrip('/')
    endpoint = f'{site_url}/wp-json/wp/v2/posts/{post_id}'
    try:
        if canonical_url:
            logger.warning(
                '[WP-SEO] wp_post_id=%s field=_yoast_wpseo_canonical decision=unchanged reason=unsupported-rest-field',
                post_id,
            )
        response = http_session.post(endpoint, json={'meta': expected}, timeout=timeout)
        if response.status_code != 200:
            raise MetadataSyncError(f'metadata_write_http_status:{response.status_code}')
        readback = http_session.get(
            endpoint,
            params={'context': 'edit', '_fields': 'id,meta'},
            timeout=timeout,
        )
        _verify_response(readback, post_id, expected, 'readback')
        _verify_response(response, post_id, None, 'write')
    except requests.RequestException as exc:
        raise MetadataSyncError(f'metadata_transport:{type(exc).__name__}') from exc
