'''Offline tests for cost aggregation, document rendering, and delivery safety.'''

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import requests

import app


class FakeResponse:
    '''Minimal mock of the HTTP response contract used by the provider clients.'''

    def __init__(self, data: dict[str, object], status_code: int = 200) -> None:
        '''Store a JSON payload and status code for deterministic tests.'''
        self.data = data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        '''Raise a standard requests error on a non-success status.'''
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}')

    def json(self) -> dict[str, object]:
        '''Return the stored JSON-compatible response.'''
        return self.data


class FakeSession:
    '''Serve queued provider responses without making HTTP requests.'''

    def __init__(self, get_responses: list[FakeResponse] | None = None,
                 post_responses: list[FakeResponse] | None = None) -> None:
        '''Configure fake GET and POST responses.'''
        self.get_responses = get_responses or []
        self.post_responses = post_responses or []
        self.get_params: list[dict[str, object]] = []
        self.post_json: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        '''Return the next GET response, retaining the sent query parameters.'''
        self.get_params.append(dict(kwargs['params']))
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        '''Return the next POST response, retaining its JSON request body.'''
        if 'json' in kwargs:
            self.post_json.append(dict(kwargs['json']))
        return self.post_responses.pop(0)


class OpenAIBillingTests(unittest.TestCase):
    '''Validate OpenAI cost aggregation and pagination.'''

    def test_pagination_and_daily_month_to_date(self) -> None:
        '''Sum the requested date only for daily, and all month buckets for MTD.'''
        session = FakeSession(get_responses=[
            FakeResponse({
                'data': [
                    {'start_time': 1791158400, 'results': [
                        {'amount': {'value': 5.0, 'currency': 'usd'}, 'line_item': 'Prior'}
                    ]},
                    {'start_time': 1791244800, 'results': [
                        {'amount': {'value': 1.1111, 'currency': 'usd'}, 'line_item': 'Tokens'}
                    ]},
                ],
                'has_more': True,
                'next_page': 'cursor-2',
            }),
            FakeResponse({
                'data': [{'start_time': 1791244800, 'results': [
                    {'amount': {'value': 2.22, 'currency': 'usd'}, 'line_item': 'Tools'}
                ]}],
                'has_more': False,
                'next_page': None,
            }),
        ])
        spend = app.fetch_openai_costs(session, 'test-secret', date(2026, 10, 6))
        self.assertEqual(spend.daily, Decimal('3.3311'))
        self.assertEqual(spend.month_to_date, Decimal('8.3311'))
        self.assertEqual(spend.daily_breakdown['Tokens'], Decimal('1.1111'))
        self.assertEqual(session.get_params[1]['page'], 'cursor-2')

    def test_invalid_currency_is_rejected(self) -> None:
        '''Never mix currencies or silently record a zero in place of spend.'''
        session = FakeSession(get_responses=[FakeResponse({
            'data': [{'start_time': 1791244800, 'results': [
                {'amount': {'value': 1, 'currency': 'eur'}}
            ]}],
            'has_more': False,
        })])
        with self.assertRaisesRegex(app.ReporterError, 'non-USD'):
            app.fetch_openai_costs(session, 'test-secret', date(2026, 10, 6))

    def test_api_error_is_not_reported_as_zero(self) -> None:
        '''Fail the report when billing data is inaccessible.'''
        session = FakeSession(get_responses=[FakeResponse({}, status_code=403)])
        with self.assertRaisesRegex(app.ReporterError, 'HTTP 403'):
            app.fetch_openai_costs(session, 'test-secret', date(2026, 10, 6))


class XAIBillingTests(unittest.TestCase):
    '''Validate xAI cost aggregation and truncation checks.'''

    def test_mtd_and_group_labels(self) -> None:
        '''Sum all USD model/service descriptions across the month.'''
        session = FakeSession(post_responses=[FakeResponse({
            'limitReached': False,
            'timeSeries': [
                {'groupLabels': ['Grok A'], 'dataPoints': [
                    {'timestamp': '2026-10-05T00:00:00Z', 'values': [3.5]},
                    {'timestamp': '2026-10-06T00:00:00Z', 'values': [1.1111]},
                ]},
                {'groupLabels': ['Grok B'], 'dataPoints': [
                    {'timestamp': '2026-10-06T00:00:00Z', 'values': [2.22]},
                ]},
            ],
        })])
        spend = app.fetch_xai_costs(session, 'management-key', 'team_123', date(2026, 10, 6))
        self.assertEqual(spend.daily, Decimal('3.3311'))
        self.assertEqual(spend.month_to_date, Decimal('6.8311'))
        self.assertEqual(spend.daily_breakdown['Grok B'], Decimal('2.22'))
        payload = session.post_json[0]['analyticsRequest']
        self.assertEqual(payload['timeRange']['timezone'], 'Etc/GMT')
        self.assertEqual(payload['values'][0]['name'], 'usd')

    def test_reject_truncated_usage(self) -> None:
        '''Reject provider data when its analytics cardinality limit was reached.'''
        session = FakeSession(post_responses=[FakeResponse({
            'limitReached': True, 'timeSeries': [],
        })])
        with self.assertRaisesRegex(app.ReporterError, 'truncated'):
            app.fetch_xai_costs(session, 'management-key', 'team_123', date(2026, 10, 6))

    def test_xai_api_error_is_not_zero(self) -> None:
        '''Recognize failed authentication as a report failure.'''
        session = FakeSession(post_responses=[FakeResponse({}, status_code=401)])
        with self.assertRaisesRegex(app.ReporterError, 'HTTP 401'):
            app.fetch_xai_costs(session, 'management-key', 'team_123', date(2026, 10, 6))


class DocumentAndDeliveryTests(unittest.TestCase):
    '''Validate generated files and idempotent notification logic.'''

    def test_synthetic_pdf_json_and_exact_aggregation(self) -> None:
        '''Generate a standalone PDF and preserve full precision in JSON.'''
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            pdf = app.run_report(date(2026, 10, 6), output, demo=True,
                                 no_send=False, force_resend=False)
            self.assertGreater(pdf.stat().st_size, 1000)
            self.assertTrue(pdf.read_bytes().startswith(b'%PDF'))
            data = json.loads(pdf.with_suffix('.json').read_text(encoding='utf-8'))
            self.assertEqual(data['data_type'], 'SYNTHETIC_DEMO')
            self.assertEqual(data['daily_total_usd'], '6.8549')
            self.assertEqual(data['month_to_date_total_usd'], '82.592')

    def test_delivery_markers_avoid_repeat_sends(self) -> None:
        '''Skip recipients whose API send was already accepted on a prior run.'''
        fake_report = app.BillingReport(
            report_date=date(2026, 10, 6),
            openai=app.ProviderSpend(Decimal('2'), Decimal('15')),
            xai=app.ProviderSpend(Decimal('3'), Decimal('18')),
            generated_at=datetime.now(timezone.utc),
        )
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', {
                 'EMAIL_TO': 'team@example.com',
                 'SMTP_HOST': 'smtp.example.com',
                 'SMTP_FROM': 'reporter@example.com',
                 'WHATSAPP_TO': '381600000000',
                 'WHATSAPP_TOKEN': 'fake-token',
                 'WHATSAPP_PHONE_NUMBER_ID': '123456789',
             }), \
             patch('app.get_live_report', return_value=fake_report), \
             patch('app.send_email') as email_mock, \
             patch('app.send_whatsapp') as whatsapp_mock:
            for _ in range(2):
                app.run_report(date(2026, 10, 6), Path(directory),
                               demo=False, no_send=False, force_resend=False)
            email_mock.assert_called_once()
            whatsapp_mock.assert_called_once()
            status = json.loads((Path(directory) / 'ai-cost-report-2026-10-06.delivery.json').read_text())
            self.assertTrue(status['email'])
            self.assertEqual(status['whatsapp'], ['381600000000'])

    def test_partial_delivery_retry_reuses_original_report(self) -> None:
        '''Retry failed channels without changing an already emailed PDF snapshot.'''
        fake_report = app.BillingReport(
            report_date=date(2026, 10, 6),
            openai=app.ProviderSpend(Decimal('3.15'), Decimal('14.2')),
            xai=app.ProviderSpend(Decimal('1.25'), Decimal('5.6')),
            generated_at=datetime.now(timezone.utc),
        )
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', {
                 'EMAIL_TO': 'team@example.com',
                 'SMTP_HOST': 'smtp.example.com',
                 'SMTP_FROM': 'reporter@example.com',
                 'WHATSAPP_TO': '381600000000',
                 'WHATSAPP_TOKEN': 'fake-token',
                 'WHATSAPP_PHONE_NUMBER_ID': '123456789',
             }), \
             patch('app.get_live_report', return_value=fake_report) as billing_mock, \
             patch('app.send_email') as email_mock, \
             patch('app.send_whatsapp', side_effect=[app.ReporterError('send failed'), None]) as wa_mock:
            with self.assertRaisesRegex(app.ReporterError, 'send failed'):
                app.run_report(date(2026, 10, 6), Path(directory),
                               demo=False, no_send=False, force_resend=False)
            pdf = Path(directory) / 'ai-cost-report-2026-10-06.pdf'
            original_bytes = pdf.read_bytes()
            app.run_report(date(2026, 10, 6), Path(directory),
                           demo=False, no_send=False, force_resend=False)
            self.assertEqual(pdf.read_bytes(), original_bytes)
            billing_mock.assert_called_once()
            email_mock.assert_called_once()
            self.assertEqual(wa_mock.call_count, 2)

    def test_missing_channels_fails_before_network(self) -> None:
        '''Do not call provider APIs when the job cannot deliver its result.'''
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', {'EMAIL_TO': '', 'WHATSAPP_TO': ''}), \
             patch('app.get_live_report') as request_mock:
            with self.assertRaisesRegex(app.ReporterError, 'Configure GOOGLE_DRIVE_ENABLED'):
                app.run_report(date(2026, 10, 6), Path(directory),
                               demo=False, no_send=False, force_resend=False)
            request_mock.assert_not_called()

    def test_optional_project_scope_sent_to_openai(self) -> None:
        '''Ensure an optional project filter is sent to the costs endpoint.'''
        session = FakeSession(get_responses=[FakeResponse({'data': [], 'has_more': False})])
        project_ids = ('proj_example123',)
        app.fetch_openai_costs(session, 'dummy-token', date(2026, 10, 6), project_ids)
        self.assertEqual(session.get_params[0]['project_ids'], list(project_ids))

    def test_drive_marker_prevents_repeated_upload(self) -> None:
        '''The same UTC report must not be repeatedly uploaded on schedule retries.'''
        fake_report = app.BillingReport(
            report_date=date(2026, 10, 6),
            openai=app.ProviderSpend(Decimal('2'), Decimal('15')),
            xai=app.ProviderSpend(Decimal('3'), Decimal('18')),
            generated_at=datetime.now(timezone.utc),
        )
        environment = {
            'GOOGLE_DRIVE_ENABLED': 'true',
            'GOOGLE_OAUTH_CLIENT_ID': 'fake-client-id',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'fake-client-secret',
            'GOOGLE_OAUTH_REFRESH_TOKEN': 'fake-refresh-token',
            'EMAIL_TO': '', 'WHATSAPP_TO': '',
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', environment), \
             patch('app.get_live_report', return_value=fake_report) as billing_mock, \
             patch('app.refresh_access_token', return_value='fake-access-token'), \
             patch('app.DrivePublisher') as publisher_class:
            publisher_class.return_value.publish.return_value = {'pdf': 'pdf-id', 'json': 'json-id'}
            for _ in range(2):
                app.run_report(date(2026, 10, 6), Path(directory), False, False, False)
            publisher_class.return_value.publish.assert_called_once()
            billing_mock.assert_called_once()
            marker = json.loads((Path(directory) / 'ai-cost-report-2026-10-06.delivery.json')
                                .read_text(encoding='utf-8'))
            self.assertTrue(marker['google_drive'])
            self.assertEqual(marker['google_drive_file_ids']['pdf'], 'pdf-id')

    def test_google_drive_partial_failure_retries_without_new_billing_query(self) -> None:
        '''A failed Drive upload can continue using the unchanged report snapshot.'''
        fake_report = app.BillingReport(
            report_date=date(2026, 10, 6),
            openai=app.ProviderSpend(Decimal('1'), Decimal('7')),
            xai=app.ProviderSpend(Decimal('2'), Decimal('9')),
            generated_at=datetime.now(timezone.utc),
        )
        environment = {
            'GOOGLE_DRIVE_ENABLED': '1',
            'GOOGLE_OAUTH_CLIENT_ID': 'fake-client-id',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'fake-client-secret',
            'GOOGLE_OAUTH_REFRESH_TOKEN': 'fake-refresh-token',
            'EMAIL_TO': '', 'WHATSAPP_TO': '',
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', environment), \
             patch('app.get_live_report', return_value=fake_report) as billing_mock, \
             patch('app.refresh_access_token', return_value='fake-access-token'), \
             patch('app.DrivePublisher') as publisher_class:
            publisher_class.return_value.publish.side_effect = [
                app.DriveDeliveryError('Simulated upload failure'),
                {'pdf': 'pdf-id', 'json': 'json-id'},
            ]
            with self.assertRaisesRegex(app.ReporterError, 'Simulated upload failure'):
                app.run_report(date(2026, 10, 6), Path(directory), False, False, False)
            pdf = Path(directory) / 'ai-cost-report-2026-10-06.pdf'
            prior_contents = pdf.read_bytes()
            app.run_report(date(2026, 10, 6), Path(directory), False, False, False)
            self.assertEqual(pdf.read_bytes(), prior_contents)
            billing_mock.assert_called_once()
            self.assertEqual(publisher_class.return_value.publish.call_count, 2)


if __name__ == '__main__':
    unittest.main()
