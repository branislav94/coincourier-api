'''Offline tests for the Google Drive OAuth + folder/file publishing logic.'''

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

import requests

from drive_delivery import DriveDeliveryError, DrivePublisher, refresh_access_token


class ResponseStub:
    '''Provide Google JSON responses without contacting a real Google account.'''

    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        '''Store a simulated HTTP response body and status code.'''
        self.body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        '''Raise on a mocked Google API failure.'''
        if self.status_code >= 400:
            raise requests.HTTPError('Request failed')

    def json(self) -> dict[str, Any]:
        '''Return simulated JSON.'''
        return self.body


class SessionStub:
    '''Queue responses and record outgoing request shapes.'''

    def __init__(self, responses: list[ResponseStub]) -> None:
        '''Initialize the response queue and request audit log.'''
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> ResponseStub:
        '''Record an API call and provide the next fake response.'''
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> ResponseStub:
        '''Handle OAuth refresh calls using the same captured queue.'''
        return self.request('POST', url, **kwargs)


class GoogleDriveTests(unittest.TestCase):
    '''Verify safe authentication, folder creation, dedupe, and partial failures.'''

    def test_refresh_token_is_posted_to_google_only(self) -> None:
        '''Refresh credentials via the official token endpoint; never echo tokens.'''
        session = SessionStub([ResponseStub({'access_token': 'fake-short-lived-token'})])
        token = refresh_access_token(session, 'client', 'secret', 'refresh')
        self.assertEqual(token, 'fake-short-lived-token')
        self.assertEqual(session.calls[0][1], 'https://oauth2.googleapis.com/token')
        self.assertEqual(session.calls[0][2]['data']['grant_type'], 'refresh_token')

    def test_bad_refresh_token_fails_safely(self) -> None:
        '''Expired or revoked refresh credentials must stop delivery, not log tokens.'''
        session = SessionStub([ResponseStub({'error': 'invalid_grant'}, status_code=400)])
        with self.assertRaisesRegex(DriveDeliveryError, 'HTTP 400') as caught:
            refresh_access_token(session, 'client', 'secret', 'not-a-real-refresh-token')
        self.assertNotIn('not-a-real-refresh-token', str(caught.exception))

    def test_creates_nested_folders_and_publishes_two_files(self) -> None:
        '''Create AI Infrastructure Costs/2026/2026-10 and upload PDF plus JSON.'''
        responses = [
            ResponseStub({'files': []}), ResponseStub({'id': 'root_id'}),
            ResponseStub({'files': []}), ResponseStub({'id': 'year_id'}),
            ResponseStub({'files': []}), ResponseStub({'id': 'month_id'}),
            ResponseStub({'files': []}), ResponseStub({'id': 'pdf_id'}),
            ResponseStub({'files': []}), ResponseStub({'id': 'json_id'}),
        ]
        session = SessionStub(responses)
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'ai-cost-report-2026-10-06.pdf'
            metadata = Path(directory) / 'ai-cost-report-2026-10-06.json'
            pdf.write_bytes(b'%PDF-fake')
            metadata.write_text('{"daily_total_usd":"3"}', encoding='utf-8')
            output = DrivePublisher(session, 'fake-token').publish(
                date(2026, 10, 6), pdf, metadata, 'AI Infrastructure Costs'
            )
        self.assertEqual(output, {'pdf': 'pdf_id', 'json': 'json_id'})
        self.assertEqual(len(session.calls), 10)
        self.assertEqual([call[0] for call in session.calls],
                         ['GET', 'POST', 'GET', 'POST', 'GET', 'POST', 'GET', 'POST', 'GET', 'POST'])
        upload = session.calls[7]
        self.assertEqual(upload[2]['params']['uploadType'], 'multipart')
        self.assertIn(b'%PDF-fake', upload[2]['data'])
        self.assertIn('multipart/related', upload[2]['headers']['Content-Type'])
        self.assertNotIn('fake-token', str(upload[2]['data']))

    def test_existing_files_are_not_reuploaded(self) -> None:
        '''A partial prior run can reuse existing Drive files without duplicates.'''
        responses = [
            ResponseStub({'files': [{'id': 'root_id'}]}),
            ResponseStub({'files': [{'id': 'year_id'}]}),
            ResponseStub({'files': [{'id': 'month_id'}]}),
            ResponseStub({'files': [{'id': 'pdf_id'}]}),
            ResponseStub({'files': [{'id': 'json_id'}]}),
        ]
        session = SessionStub(responses)
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'ai-cost-report-2026-10-06.pdf'
            record = Path(directory) / 'ai-cost-report-2026-10-06.json'
            pdf.write_bytes(b'%PDF-test')
            record.write_text('{}', encoding='utf-8')
            result = DrivePublisher(session, 'token').publish(
                date(2026, 10, 6), pdf, record, 'AI Infrastructure Costs'
            )
        self.assertEqual(result, {'pdf': 'pdf_id', 'json': 'json_id'})
        self.assertEqual(len(session.calls), 5)
        self.assertTrue(all(call[0] == 'GET' for call in session.calls))

    def test_duplicate_remote_names_are_rejected(self) -> None:
        '''Never randomly target one of two same-named Drive folders.'''
        session = SessionStub([ResponseStub({'files': [{'id': '1'}, {'id': '2'}]})])
        with self.assertRaisesRegex(DriveDeliveryError, 'duplicate'):
            DrivePublisher(session, 'token').ensure_folder('root', 'AI Infrastructure Costs')

    def test_force_replace_updates_files_instead_of_creating_duplicates(self) -> None:
        '''Reconciliations can update the same Drive file instead of duplicating it.'''
        session = SessionStub([
            ResponseStub({'files': [{'id': 'pdf_id'}]}), ResponseStub({'id': 'pdf_id'}),
        ])
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'report.pdf'
            pdf.write_bytes(b'%PDF-new')
            file_id = DrivePublisher(session, 'token').upsert_file(
                'existing_folder', pdf, 'application/pdf', replace=True
            )
        self.assertEqual(file_id, 'pdf_id')
        self.assertEqual(session.calls[1][0], 'PATCH')
        self.assertEqual(session.calls[1][2]['params']['uploadType'], 'media')


if __name__ == '__main__':
    unittest.main()
