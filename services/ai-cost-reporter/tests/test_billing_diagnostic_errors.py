'''Offline verification of structured billing failures without credential or response disclosure.'''

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from unittest.mock import Mock, patch

import requests

import app
from test_billing_correctness import (
    ENVIRONMENT, REPORT_DATE, RawResponse, Session, bucket, openai_page, record, series, xai_body,
)


class StructuredErrorTests(unittest.TestCase):
    '''Keep legacy exceptions compatible and diagnostic metadata bounded and safe.'''

    def test_legacy_exception_message_and_default_contract_are_preserved(self) -> None:
        '''Existing caller text and RuntimeError behavior remain unchanged.'''
        error = app.ReporterError('Existing report failure')
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'Existing report failure')
        self.assertEqual(error.args, ('Existing report failure',))
        self.assertEqual(error.code, 'internal_error')
        self.assertEqual(error.failure_stage, 'unknown')
        self.assertEqual(error.stage, error.failure_stage)
        self.assertEqual(error.details, {})

    def test_metadata_never_accepts_free_form_strings_or_unknown_fields(self) -> None:
        '''Raw provider material cannot enter the machine-safe details dictionary.'''
        details = {
            'http_status': 403,
            'expected_day_count': 6,
            'observed_day_count': 5,
            'missing_utc_days': ['2026-10-06', '2026-10-02', '2026-10-06'],
            'record_count': True,
            'page_count': -1,
            'max_page_count': 1000000001,
            'response_bytes': 'offline-private-body-marker',
            'body': 'offline-private-body-marker',
            'credential': 'offline-private-credential-marker',
            'url': 'https://example.invalid/offline-private-credential-marker',
        }
        error = app.ReporterError('Legacy text', code='incomplete_coverage', stage='coverage', details=details)
        self.assertEqual(error.details, {'http_status': 403, 'expected_day_count': 6,
                                         'observed_day_count': 5,
                                         'missing_utc_days': ['2026-10-02', '2026-10-06']})
        details['missing_utc_days'].append('2026-10-01')
        self.assertEqual(error.details['missing_utc_days'], ['2026-10-02', '2026-10-06'])
        self.assertNotIn('offline-private', repr(error.details))

    def test_invalid_codes_stages_and_dates_are_safely_discarded(self) -> None:
        '''Unknown labels and malformed dates cannot smuggle strings into metadata.'''
        for days in (['offline-private-marker'], ['2026-02-30'], ['1999-12-31'],
                     ['2026-10-06\nmarker'], ['2026-10-06', None], ['2026-10-06'] * 32):
            with self.subTest(days=days):
                error = app.ReporterError('Legacy text', code='offline-private-marker',
                                          stage='offline-private-marker', details={'missing_utc_days': days})
                self.assertEqual(error.code, 'internal_error')
                self.assertEqual(error.stage, 'unknown')
                self.assertEqual(error.details, {})
        for status in (True, 99, 600, '401'):
            self.assertEqual(app.ReporterError('Failure', details={'http_status': status}).details, {})


class DiagnosticPreflightTests(unittest.TestCase):
    '''Permit independent diagnosis without weakening the combined production preflight.'''

    def test_openai_configuration_is_independent_and_preserves_selected_scope(self) -> None:
        '''Missing xAI settings cannot hide the configured OpenAI project filter.'''
        environment = {'OPENAI_ADMIN_KEY': 'offline-openai-placeholder',
                       'OPENAI_PROJECT_IDS': 'proj_a,proj_b,proj_a'}
        with patch.dict('os.environ', environment, clear=True):
            configuration = app.validate_openai_billing_configuration(REPORT_DATE)
        self.assertEqual(configuration.admin_key, environment['OPENAI_ADMIN_KEY'])
        self.assertEqual(configuration.project_ids, ('proj_a', 'proj_b'))
        self.assertNotIn(environment['OPENAI_ADMIN_KEY'], repr(configuration))

    def test_xai_configuration_and_key_preflight_do_not_require_openai(self) -> None:
        '''Team-scoped reads validate team, while key-validation preflight needs only the key.'''
        with patch.dict('os.environ', {'XAI_MANAGEMENT_KEY': 'offline-xai-placeholder',
                                      'XAI_TEAM_ID': 'team_offline'}, clear=True):
            configuration = app.validate_xai_billing_configuration(REPORT_DATE)
        self.assertEqual(configuration.team_id, 'team_offline')
        self.assertNotIn('offline-xai-placeholder', repr(configuration))
        with patch.dict('os.environ', {'XAI_MANAGEMENT_KEY': 'offline-xai-placeholder'}, clear=True):
            self.assertEqual(app.billing_header_key('XAI_MANAGEMENT_KEY'), 'offline-xai-placeholder')
            with self.assertRaises(app.ReporterError) as raised:
                app.validate_xai_billing_configuration(REPORT_DATE)
        self.assertEqual((raised.exception.code, raised.exception.stage), ('configuration_missing', 'preflight'))

    def test_key_reader_is_limited_to_billing_environment_names(self) -> None:
        '''An unsupported variable is rejected before any environment lookup.'''
        with patch('app.os.environ.get', side_effect=AssertionError('Unexpected environment lookup')):
            with self.assertRaises(app.ReporterError) as raised:
                app.billing_header_key('OTHER_PRIVATE_VARIABLE')
        self.assertEqual((raised.exception.code, raised.exception.stage), ('invalid_config', 'preflight'))
        self.assertNotIn('OTHER_PRIVATE_VARIABLE', str(raised.exception))

    def test_configuration_failure_categories_precede_network_access(self) -> None:
        '''Missing, malformed and unsafe provider input is identified without a request.'''
        for variable, value, code in (('OPENAI_ADMIN_KEY', '', 'configuration_missing'),
                                      ('XAI_MANAGEMENT_KEY', '', 'configuration_missing'),
                                      ('XAI_TEAM_ID', '', 'configuration_missing'),
                                      ('XAI_TEAM_ID', 'team/path', 'invalid_config'),
                                      ('OPENAI_PROJECT_IDS', 'proj_a,', 'invalid_config'),
                                      ('OPENAI_ADMIN_KEY', 'offline\nheader', 'invalid_config'),
                                      ('XAI_MANAGEMENT_KEY', 'offline\rheader', 'invalid_config')):
            session = Mock()
            environment = dict(ENVIRONMENT, **{variable: value})
            with self.subTest(variable=variable, code=code), patch.dict('os.environ', environment, clear=True):
                with self.assertRaises(app.ReporterError) as raised:
                    app.get_live_report(session, REPORT_DATE)
            self.assertEqual((raised.exception.code, raised.exception.stage), (code, 'preflight'))
            self.assertEqual(raised.exception.details, {})
            session.get.assert_not_called()
            session.post.assert_not_called()

    def test_invalid_date_is_identified_before_provider_configuration_reads(self) -> None:
        '''A partial UTC day or unsupported date does not prompt a key lookup.'''
        for day in (date(1999, 12, 31), datetime.now(timezone.utc).date(),
                    datetime.now(timezone.utc).date() + timedelta(days=1)):
            for validator in (app.validate_openai_billing_configuration, app.validate_xai_billing_configuration):
                with self.subTest(day=day, validator=validator.__name__), \
                        patch('app.os.environ.get', side_effect=AssertionError('Unexpected environment lookup')):
                    with self.assertRaises(app.ReporterError) as raised:
                        validator(day)
                self.assertEqual((raised.exception.code, raised.exception.stage), ('invalid_report_date', 'preflight'))


class BillingReaderErrorTests(unittest.TestCase):
    '''Exercise actual strict billing readers and their sanitized diagnostic classifications.'''

    def failure(self, operation: Callable[[], Any], code: str, stage: str) -> app.ReporterError:
        '''Return the failure after asserting the stable machine-readable contract.'''
        with self.assertRaises(app.ReporterError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.failure_stage, stage)
        return raised.exception

    def openai(self, body: dict[str, Any] | bytes, **kwargs: Any) -> app.ProviderSpend:
        '''Run one offline OpenAI reader with documented response bytes.'''
        return app.fetch_openai_costs(Session(gets=[RawResponse(body)]), 'offline-key', REPORT_DATE, **kwargs)

    def xai(self, body: dict[str, Any] | bytes) -> app.ProviderSpend:
        '''Run one offline xAI analytics reader.'''
        responses = [RawResponse(body)]
        if isinstance(body, dict) and body.get('limitReached') is False and body.get('timeSeries') == []:
            responses.append(RawResponse(xai_body([])))
        return app.fetch_xai_costs(Session(posts=responses), 'offline-key', 'team_offline', REPORT_DATE)

    def test_http_errors_include_only_integer_status_not_provider_body(self) -> None:
        '''Authentication, permission and provider failures expose no response content.'''
        for status in (400, 401, 403, 422, 429, 500, 503):
            error = self.failure(lambda: app.response_json(
                RawResponse(b'{"private":"offline-provider-body-marker"}', status), 'OpenAI'),
                'http_error', 'http')
            self.assertEqual(error.details, {'http_status': status})
            self.assertNotIn('offline-provider-body-marker', str(error))

    def test_network_failures_have_no_sensitive_chained_transport_text(self) -> None:
        '''Both provider readers replace untrusted exception text with a fixed failure.'''
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline-private-network-marker')
        session.post.side_effect = requests.ConnectionError('offline-private-network-marker')
        operations = (lambda: app.fetch_openai_costs(session, 'offline-key', REPORT_DATE),
                      lambda: app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE))
        for operation in operations:
            error = self.failure(operation, 'network_error', 'network')
            self.assertEqual(error.details, {})
            self.assertNotIn('offline-private-network-marker', str(error))
            self.assertTrue(error.__suppress_context__)

    def test_invalid_json_and_response_schema_have_distinct_categories(self) -> None:
        '''Invalid numeric JSON and valid JSON with unsupported structure are distinguished.'''
        for content in (b'', b'{', b'{"cost":NaN}', b'{"cost":1,"cost":2}'):
            self.failure(lambda: app.response_json(RawResponse(content), 'OpenAI'), 'invalid_json', 'response')
        for operation in (lambda: self.openai({'object': 'wrong'}),
                          lambda: self.openai({'object': 'page', 'data': 'offline-private-marker'}),
                          lambda: self.xai({'limitReached': False}),
                          lambda: self.xai({'timeSeries': []}),
                          lambda: app.response_json(RawResponse(b'[]'), 'OpenAI')):
            error = self.failure(operation, 'response_schema_invalid', 'schema')
            self.assertEqual(error.details, {})
            self.assertNotIn('offline-private-marker', str(error))

    def test_response_and_record_safety_limits_are_identified_without_amounts(self) -> None:
        '''Bounded diagnostic counts do not expose accounting records or spend values.'''
        with patch.object(app, 'MAX_BILLING_RESPONSE_BYTES', 8):
            error = self.failure(lambda: app.response_json(RawResponse(b' ' * 9), 'OpenAI'),
                                 'response_truncated', 'response')
        self.assertEqual(error.details, {'response_bytes': 9, 'max_response_bytes': 8})
        body = openai_page()
        body['data'][5]['results'] = [record('1', 'A'), record('2', 'B')]
        with patch.object(app, 'MAX_BILLING_ROWS', 1):
            error = self.failure(lambda: self.openai(body), 'response_truncated', 'response')
        self.assertEqual(error.details, {'record_count': 2, 'max_record_count': 1})
        with patch.object(app, 'MAX_BILLING_ROWS', 1):
            self.failure(lambda: app.exact_sum((Decimal('1'), Decimal('2'))),
                         'response_truncated', 'aggregation')

    def test_openai_missing_coverage_reports_exact_requested_dates(self) -> None:
        '''A missing beginning or end day is reported as a date rather than a zero cost.'''
        body = openai_page([bucket(day) for day in range(2, 6)])
        error = self.failure(lambda: self.openai(body), 'incomplete_coverage', 'coverage')
        self.assertEqual(error.details, {'missing_utc_days': ['2026-10-01', '2026-10-06'],
                                         'expected_day_count': 6, 'observed_day_count': 4})
        error = self.failure(lambda: self.openai(openai_page([])), 'incomplete_coverage', 'coverage')
        self.assertEqual(error.details['missing_utc_days'], [f'2026-10-{day:02d}' for day in range(1, 7)])

    def test_xai_dense_coverage_and_inactive_collections_remain_distinct(self) -> None:
        '''Sparse groups fail; two explicit absent collections preserve provisional metadata.'''
        group = series('offline-private-description-marker')
        group['dataPoints'].pop(2)
        error = self.failure(lambda: self.xai(xai_body([group])), 'incomplete_coverage', 'coverage')
        self.assertEqual(error.details, {'missing_utc_days': ['2026-10-03'],
                                         'expected_day_count': 6, 'observed_day_count': 5})
        self.assertNotIn('offline-private-description-marker', repr(error.details))
        inactive = self.xai(xai_body([]))
        self.assertEqual(inactive.accounting_state, app.NO_RECORDED_USAGE)
        self.assertEqual(inactive.reconciliation_status, 'not_reconciled')
        self.assertIs(inactive.allow_recheck, True)
        self.assertEqual(inactive.daily_breakdown, {})
        self.failure(lambda: self.xai({'limitReached': True, 'timeSeries': []}),
                     'response_truncated', 'coverage')

    def test_pagination_invalid_and_page_limit_are_distinguished(self) -> None:
        '''An incomplete cursor state differs from a bounded pagination exhaustion.'''
        body = openai_page()
        body.pop('has_more')
        self.failure(lambda: self.openai(body), 'pagination_invalid', 'pagination')
        body = openai_page([bucket(1)], more=True, cursor='offline-cursor')
        with patch.object(app, 'MAX_BILLING_PAGES', 1):
            error = self.failure(lambda: self.openai(body), 'pagination_limit', 'pagination')
        self.assertEqual(error.details, {'page_count': 1, 'max_page_count': 1})

    def test_scope_mismatch_duplicate_and_out_of_range_day_are_classified(self) -> None:
        '''Scope and accounting identity violations stay strict without revealing records.'''
        body = openai_page()
        body['data'][5]['results'] = [record('1', project='proj_unrequested')]
        error = self.failure(lambda: self.openai(body, project_ids=('proj_expected',)), 'scope_mismatch', 'scope')
        self.assertEqual(error.details, {})
        body = openai_page([bucket(1), bucket(1)])
        self.failure(lambda: self.openai(body), 'duplicate_data', 'coverage')
        body = openai_page()
        body['data'][5] = bucket(7)
        self.failure(lambda: self.openai(body), 'out_of_range_day', 'coverage')
        group = series()
        group['dataPoints'][5]['timestamp'] = '2026-10-07T00:00:00Z'
        self.failure(lambda: self.xai(xai_body([group])), 'out_of_range_day', 'coverage')

    def test_invalid_usd_and_unsupported_currency_are_distinct(self) -> None:
        '''Invalid or unbounded numeric amounts remain rejected without raw value disclosure.'''
        for value in (None, True, '1_000', '1e999999999', 'offline-private-value-marker'):
            body = openai_page()
            body['data'][5]['results'] = [record(value)]
            error = self.failure(lambda: self.openai(body), 'invalid_usd_amount', 'amount')
            self.assertEqual(error.details, {})
            self.assertNotIn('offline-private-value-marker', str(error))
        body = openai_page()
        body['data'][5]['results'] = [record('1')]
        body['data'][5]['results'][0]['amount']['currency'] = 'EUR'
        self.failure(lambda: self.openai(body), 'unsupported_currency', 'amount')

    def test_strict_successful_zero_and_high_precision_accounting_are_unchanged(self) -> None:
        '''Explicit complete zeros succeed; new diagnostic metadata does not round actual values.'''
        self.assertEqual(self.openai(openai_page()).daily, Decimal('0'))
        self.assertEqual(self.xai(xai_body()).daily, Decimal('0'))
        body = openai_page()
        body['data'][5]['results'] = [record('0.123456789012345678901234567890123456789')]
        self.assertEqual(self.openai(body).daily, Decimal('0.123456789012345678901234567890123456789'))


if __name__ == '__main__':
    unittest.main()
