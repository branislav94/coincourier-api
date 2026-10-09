'''Upload completed cost reports to an app-managed Google Drive folder.'''

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import requests


OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
DRIVE_API_BASE = 'https://www.googleapis.com/drive/v3/files'
DRIVE_UPLOAD_BASE = 'https://www.googleapis.com/upload/drive/v3/files'
FOLDER_MIME = 'application/vnd.google-apps.folder'


class DriveDeliveryError(RuntimeError):
    '''An upload/authentication failure that must stop successful publication.'''


def _json_object(response: requests.Response, purpose: str) -> Mapping[str, Any]:
    '''Validate a Google API response without logging tokens or response bodies.'''
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise DriveDeliveryError(
            f'Google {purpose} failed: HTTP {response.status_code}'
        ) from error
    try:
        data = response.json()
    except ValueError as error:
        raise DriveDeliveryError(f'Google {purpose} returned invalid JSON') from error
    if not isinstance(data, dict):
        raise DriveDeliveryError(f'Google {purpose} returned an invalid object')
    return data


def refresh_access_token(
    session: requests.Session, client_id: str, client_secret: str, refresh_token: str
) -> str:
    '''Exchange the OAuth refresh token for a short-lived Google access token.'''
    if not client_id or not client_secret or not refresh_token:
        raise DriveDeliveryError('Missing GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN')
    try:
        response = session.post(
            OAUTH_TOKEN_URL,
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token',
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise DriveDeliveryError('Google OAuth token refresh failed: network error') from error
    data = _json_object(response, 'OAuth token refresh')
    token = data.get('access_token')
    if not isinstance(token, str) or not token:
        raise DriveDeliveryError('Google OAuth token response omitted access_token')
    return token


class DrivePublisher:
    '''Idempotently place PDF/JSON into My Drive under year/month folders.'''

    def __init__(self, session: requests.Session, access_token: str) -> None:
        '''Create the client with a short-lived OAuth bearer token.'''
        if not access_token:
            raise DriveDeliveryError('Missing Google OAuth access token')
        self.session = session
        self.headers = {'Authorization': f'Bearer {access_token}'}

    def _request(
        self, method: str, url: str, purpose: str, **kwargs: Any
    ) -> Mapping[str, Any]:
        '''Make one Drive request; do not retry non-idempotent uploads blindly.'''
        try:
            response = self.session.request(
                method, url, headers={**self.headers, **kwargs.pop('headers', {})},
                timeout=45, **kwargs
            )
        except requests.RequestException as error:
            raise DriveDeliveryError(f'Google Drive {purpose} failed: network error') from error
        return _json_object(response, f'Drive {purpose}')

    def find_child(self, parent_id: str, name: str, mime_type: str) -> str | None:
        '''Return the ID of an app-created file with exact parent/name/type.'''
        if parent_id != 'root' and not re.fullmatch(r'[\w-]{1,256}', parent_id):
            raise DriveDeliveryError('Google Drive parent folder ID has an invalid format')
        escaped_name = name.replace('\\', '\\\\').replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and name = '{escaped_name}' "
            f"and mimeType = '{mime_type}' and trashed = false"
        )
        next_page: str | None = None
        found: list[str] = []
        visited: set[str] = set()
        while True:
            params: dict[str, Any] = {
                'q': query,
                'fields': 'nextPageToken,files(id,name,mimeType)',
                'pageSize': 100,
            }
            if next_page:
                params['pageToken'] = next_page
            data = self._request('GET', DRIVE_API_BASE, 'search', params=params)
            files = data.get('files')
            if not isinstance(files, list):
                raise DriveDeliveryError('Google Drive search response omitted files')
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    raise DriveDeliveryError('Google Drive search returned an invalid file')
                found.append(item['id'])
            page = data.get('nextPageToken')
            if not page:
                break
            if not isinstance(page, str) or page in visited:
                raise DriveDeliveryError('Google Drive search pagination cursor is invalid')
            visited.add(page)
            next_page = page
            if len(visited) > 100:
                raise DriveDeliveryError('Google Drive search exceeded pagination safety limit')
        if len(found) > 1:
            raise DriveDeliveryError('Google Drive contains duplicate app-created report items')
        return found[0] if found else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        '''Find a same-name app-created folder, or create one privately.'''
        existing = self.find_child(parent_id, name, FOLDER_MIME)
        if existing:
            return existing
        metadata = {'name': name, 'mimeType': FOLDER_MIME}
        if parent_id != 'root':
            metadata['parents'] = [parent_id]
        data = self._request(
            'POST', DRIVE_API_BASE, 'folder creation',
            params={'fields': 'id,name'}, json=metadata,
        )
        folder_id = data.get('id')
        if not isinstance(folder_id, str) or not folder_id:
            raise DriveDeliveryError('Google Drive folder creation returned no ID')
        return folder_id

    def upsert_file(
        self, folder_id: str, path: Path, mime_type: str, replace: bool
    ) -> str:
        '''Upload the file once, or replace content on explicit --force-resend.'''
        if not path.is_file():
            raise DriveDeliveryError(f'Report artifact is missing: {path.name}')
        current = self.find_child(folder_id, path.name, mime_type)
        if current and not replace:
            return current
        content = path.read_bytes()
        if current:
            data = self._request(
                'PATCH', f'{DRIVE_UPLOAD_BASE}/{current}', 'file update',
                params={'uploadType': 'media', 'fields': 'id'},
                headers={'Content-Type': mime_type}, data=content,
            )
        else:
            boundary = f'ai-cost-reporter-{uuid.uuid4().hex}'
            metadata = json.dumps({'name': path.name, 'parents': [folder_id]}).encode('utf-8')
            body = (
                f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode('ascii')
                + metadata
                + f'\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n'.encode('ascii')
                + content
                + f'\r\n--{boundary}--\r\n'.encode('ascii')
            )
            data = self._request(
                'POST', DRIVE_UPLOAD_BASE, 'file creation',
                params={'uploadType': 'multipart', 'fields': 'id'},
                headers={'Content-Type': f'multipart/related; boundary={boundary}'},
                data=body,
            )
        file_id = data.get('id')
        if not isinstance(file_id, str) or not file_id:
            raise DriveDeliveryError('Google Drive upload response omitted file ID')
        return file_id

    def publish(
        self, report_date: date, pdf_path: Path, json_path: Path,
        root_folder_name: str, replace: bool = False,
    ) -> dict[str, str]:
        '''Publish both artifacts under AI Infrastructure Costs/YYYY/YYYY-MM.'''
        if not root_folder_name or len(root_folder_name) > 200:
            raise DriveDeliveryError('GOOGLE_DRIVE_FOLDER_NAME must contain 1-200 characters')
        root = self.ensure_folder('root', root_folder_name)
        year = self.ensure_folder(root, str(report_date.year))
        month = self.ensure_folder(year, report_date.strftime('%Y-%m'))
        return {
            'pdf': self.upsert_file(month, pdf_path, 'application/pdf', replace),
            'json': self.upsert_file(month, json_path, 'application/json', replace),
        }
