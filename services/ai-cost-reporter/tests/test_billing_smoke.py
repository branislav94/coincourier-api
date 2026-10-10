'''Offline regression tests for the read-only billing smoke helper.'''

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import requests

import app


SERVICE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('billing_smoke_helper', SERVICE_ROOT / 'scripts' / 'billing_smoke_test.py')
if SPEC is None or SPEC.loader is None:
    raise RuntimeError('Billing smoke helper could not be loaded')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

REPORT_DATE = date(2026, 10, 6)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
ENVIRONMENT = {
    'OPENAI_ADMIN_KEY': 'unit-openai-key',
    'XAI_MANAGEMENT_KEY': 'unit-xai-key',
    'XAI_TEAM_ID': 'team_unit',
    'OPENAI_PROJECT_IDS': '',
}


class FixedDatetime(datetime):
    '''Keep complete-day checks independent of the execution host clock.'''

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        '''Return the fixed instant in the requested timezone.'''
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ResponseStub:
    '''Provide provider-shaped JSON to the actual billing parsers without a network.'''

    def __init__(self, body: Any, status_code: int = 200) -> None:
        '''Store response bytes, status and resource state.'''
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.status_code = status_code
        self.headers = {'Content-Type': 'application/json'}
        self.closed = False

    @property
    def text(self) -> str:
        '''Expose UTF-8 response text for compatible strict JSON parsing.'''
        return self.content.decode('utf-8')

    def raise_for_status(self) -> None:
        '''Simulate HTTP failure with untrusted text that must never reach stdout.'''
        if self.status_code >= 400:
            raise requests.HTTPError('Sensitive mocked request details must not be printed')

    def json(self, **kwargs: Any) -> Any:
        '''Honor strict numeric/object parser arguments used by the reporter.'''
        return json.loads(self.content, **kwargs)

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        '''Support bounded parsing if the provider implementation streams responses.'''
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self) -> None:
        '''Record cleanup after a redirected or rejected response.'''
        self.closed = True


def provider_responses() -> list[ResponseStub]:
    '''Return complete month-to-date UTC records with explicit provider-recorded values.'''
    buckets = []
    points = []
    for day in range(1, 7):
        start = datetime(2026, 10, day, tzinfo=timezone.utc)
        buckets.append({
            'object': 'bucket', 'start_time': int(start.timestamp()),
            'end_time': int((start + timedelta(days=1)).timestamp()),
            'results': [{'object': 'organization.costs.result',
                         'amount': {'value': '0.1000000000000000001' if day == 6 else '1',
                                    'currency': 'usd'},
                         'line_item': 'synthetic mocked billing item'}],
        })
        points.append({'timestamp': start.isoformat().replace('+00:00', 'Z'),
                       'values': ['0.2000000000000000002' if day == 6 else '2']})
    return [ResponseStub({'object': 'page', 'data': buckets, 'has_more': False, 'next_page': None}),
            ResponseStub({'limitReached': False,
                          'timeSeries': [{'groupLabels': ['synthetic mocked USD billing'],
                                          'dataPoints': points}]})]


class BillingSmokeTests(unittest.TestCase):
    '''Require explicit live opt-in, safe preflight and exact complete cost summaries.'''

    def setUp(self) -> None:
        '''Use only synthetic environment values and a fixed UTC clock.'''
        environment = patch.dict(os.environ, ENVIRONMENT, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for module in (app, smoke):
            clock = patch.object(module, 'datetime', FixedDatetime)
            clock.start()
            self.addCleanup(clock.stop)

    def run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        '''Capture safe stdout/stderr independently for CLI assertions.'''
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = smoke.main(arguments)
        return status, output.getvalue(), errors.getvalue()

    def test_default_plan_never_reads_credentials_or_starts_a_session(self) -> None:
        '''Offline planning works even if every billing environment lookup is forbidden.'''
        original_get = os.environ.get

        def guarded_get(name: str, default: Any = None) -> Any:
            '''Permit terminal defaults but reject any billing configuration lookup.'''
            if name in ENVIRONMENT:
                raise AssertionError('Offline planning must not inspect billing configuration')
            return original_get(name, default)

        with patch.object(os.environ, 'get', side_effect=guarded_get), \
             patch.object(smoke, 'ReadOnlyBillingSession') as session, \
             patch.object(app, 'get_live_report') as fetch:
            status, output, errors = self.run_main(['--date', '2026-10-06'])
            session.assert_not_called()
            fetch.assert_not_called()
        self.assertEqual(status, 0)
        self.assertEqual(errors, '')
        plan = json.loads(output)
        self.assertEqual(plan['status'], 'prepared_offline')
        self.assertFalse(plan['network_requests'])
        self.assertEqual([item['method'] for item in plan['requests']], ['GET', 'POST'])
        self.assertNotIn(ENVIRONMENT['OPENAI_ADMIN_KEY'], output)
        self.assertNotIn(ENVIRONMENT['XAI_MANAGEMENT_KEY'], output)

    def test_utc_period_metadata_has_full_day_and_exclusive_next_midnight(self) -> None:
        '''Job timezone cannot shorten the requested UTC daily/MTD periods.'''
        status, output, _errors = self.run_main(['--date', '2026-10-06'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)['periods'], {
            'daily': {'start_utc': '2026-10-06T00:00:00Z',
                      'end_utc_exclusive': '2026-10-07T00:00:00Z'},
            'month_to_date': {'start_utc': '2026-10-01T00:00:00Z',
                              'end_utc_exclusive': '2026-10-07T00:00:00Z'},
        })
        status, output, _errors = self.run_main([])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)['report_date_utc'], '2026-10-09')

    def test_actual_mocked_provider_reads_preserve_exact_costs_and_safe_transport(self) -> None:
        '''Exercise both real provider parsers through the guarded session using mock HTTP.'''
        responses = provider_responses()
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> ResponseStub:
            '''Audit fixed endpoints and TLS/redirect settings without connecting anywhere.'''
            self.assertFalse(session.trust_env)
            calls.append((method, url, kwargs))
            return responses.pop(0)

        with patch.object(requests.Session, 'request', autospec=True, side_effect=request):
            status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
        self.assertEqual(status, 0, errors)
        self.assertEqual(errors, '')
        result = json.loads(output)
        self.assertEqual(result['generated_at_utc'], NOW.isoformat())
        self.assertEqual(result['daily_total_usd'], '0.3000000000000000003')
        self.assertEqual(result['month_to_date_total_usd'], '15.3000000000000000003')
        self.assertEqual(result['providers']['openai']['daily_usd'], '0.1000000000000000001')
        self.assertEqual(result['providers']['xai']['daily_usd'], '0.2000000000000000002')
        self.assertEqual(result['openai_scope'], {'type': 'all_organization_projects', 'project_ids': []})
        self.assertEqual(result['xai_scope']['team_id'], 'team_unit')
        self.assertEqual([(method, url) for method, url, _kwargs in calls], [
            ('GET', app.OPENAI_COSTS_URL),
            ('POST', app.XAI_BASE_URL + '/v1/billing/teams/team_unit/usage'),
        ])
        self.assertTrue(all(kwargs['verify'] is True for _method, _url, kwargs in calls))
        self.assertTrue(all(kwargs['allow_redirects'] is False for _method, _url, kwargs in calls))
        self.assertEqual(calls[1][2]['json']['analyticsRequest']['timeRange']['endTime'],
                         '2026-10-07 00:00:00')
        self.assertNotIn(ENVIRONMENT['OPENAI_ADMIN_KEY'], output)
        self.assertNotIn(ENVIRONMENT['XAI_MANAGEMENT_KEY'], output)
        self.assertNotIn('synthetic mocked billing item', output)

    def test_all_invalid_live_configuration_fails_before_session_or_provider_calls(self) -> None:
        '''A missing second key or invalid team/scope cannot trigger the first billing API.'''
        invalid_environments = [
            {'OPENAI_ADMIN_KEY': ''}, {'XAI_MANAGEMENT_KEY': ''}, {'XAI_TEAM_ID': ''},
            {'XAI_TEAM_ID': '../unsafe'}, {'OPENAI_PROJECT_IDS': 'invalid-project'},
            {'OPENAI_ADMIN_KEY': 'unit\ninjected'}, {'XAI_MANAGEMENT_KEY': 'unit\rinjected'},
        ]
        for changes in invalid_environments:
            with self.subTest(changes=list(changes)), patch.dict(os.environ, changes), \
                 patch.object(smoke, 'ReadOnlyBillingSession') as session, \
                 patch.object(app, 'get_live_report') as fetch:
                status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
                session.assert_not_called()
                fetch.assert_not_called()
            self.assertEqual(status, 1)
            self.assertEqual(output, '')
            self.assertEqual(errors, 'BILLING_SMOKE_FAILED: no complete cost read was confirmed\n')
            self.assertNotIn('injected', errors)

    def test_invalid_day_and_arguments_fail_safely_without_network(self) -> None:
        '''Reject today's UTC date, ancient/incomplete dates and unsafe argument values.'''
        for arguments in (['--live', '--date', '2026-10-10'], ['--date', '1999-12-31'],
                          ['--date', 'not-a-date-sensitive-value'],
                          ['--unexpected-secret-argument', 'sensitive-value']):
            with patch.object(smoke, 'ReadOnlyBillingSession') as session:
                status, output, errors = self.run_main(arguments)
                session.assert_not_called()
            self.assertEqual(status, 1)
            self.assertEqual(output, '')
            self.assertNotIn('sensitive-value', errors)
            self.assertNotIn('Traceback', errors)

    def test_second_provider_failure_never_prints_partial_or_zero_totals(self) -> None:
        '''A successful OpenAI read plus failed xAI read cannot become a cost report.'''
        responses = [provider_responses()[0], ResponseStub({'private': 'sensitive'}, status_code=403)]

        def request(*args: Any, **kwargs: Any) -> ResponseStub:
            '''Return one successful mocked read, then a failed second provider.'''
            return responses.pop(0)

        with patch.object(requests.Session, 'request', autospec=True, side_effect=request) as transport:
            status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertNotIn('daily', errors)
        self.assertNotIn('Sensitive', errors)
        self.assertNotIn('Traceback', errors)

    def test_redirects_are_refused_without_forwarding_credentials(self) -> None:
        '''Even a provider HTTP 302 cannot send Authorization to another endpoint.'''
        response = ResponseStub({}, status_code=302)
        response.headers['Location'] = 'https://attacker.invalid/leak'
        with patch.object(requests.Session, 'request', return_value=response) as transport:
            status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        transport.assert_called_once()
        self.assertTrue(response.closed)
        self.assertNotIn('attacker', errors)
        self.assertNotIn(ENVIRONMENT['OPENAI_ADMIN_KEY'], errors)

    def test_session_only_allows_fixed_read_only_billing_endpoints(self) -> None:
        '''Block provider mutations, wrong hosts and GET substitutions for xAI analytics.'''
        disallowed = [
            ('DELETE', app.OPENAI_COSTS_URL), ('POST', app.OPENAI_COSTS_URL),
            ('GET', app.XAI_BASE_URL + '/v1/billing/teams/team_unit/usage'),
            ('POST', app.XAI_BASE_URL + '/v1/billing/teams/other_team/usage'),
            ('POST', 'https://script.google.com/macros/s/unit/exec'),
            ('GET', 'https://api.openai.com.attacker.invalid/v1/organization/costs'),
        ]
        with smoke.ReadOnlyBillingSession('team_unit') as session, \
             patch.object(requests.Session, 'request') as transport:
            for method, url in disallowed:
                with self.subTest(method=method), self.assertRaises(smoke.BillingSmokeError):
                    session.request(method, url)
            transport.assert_not_called()

    def test_exact_summary_and_scope_are_stable_under_low_decimal_context_precision(self) -> None:
        '''No cents formatting or ambient Decimal precision may alter reconciliation amounts.'''
        report = app.BillingReport(
            report_date=REPORT_DATE,
            openai=app.ProviderSpend(Decimal('123456789012345678901234567890.0000000000000000001'),
                                     Decimal('123456789012345678901234567890.0000000000000000001')),
            xai=app.ProviderSpend(Decimal('0.0000000000000000002'), Decimal('0.0000000000000000002')),
            generated_at=NOW, openai_project_ids=('proj_unitA',),
        )
        with patch.dict(os.environ, {'OPENAI_PROJECT_IDS': 'proj_unitA,proj_unitA'}), \
             patch.object(app, 'get_live_report', return_value=report), localcontext() as context:
            context.prec = 6
            status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
        self.assertEqual(status, 0, errors)
        summary = json.loads(output)
        self.assertEqual(summary['daily_total_usd'],
                         '123456789012345678901234567890.0000000000000000003')
        self.assertEqual(summary['openai_scope'], {'type': 'selected_projects', 'project_ids': ['proj_unitA']})

    def test_exponent_amounts_render_identically_across_ambient_decimal_contexts(self) -> None:
        '''Both provider totals stay lossless and fixed-point even when E/e defaults differ.'''
        report = app.BillingReport(
            REPORT_DATE,
            app.ProviderSpend(Decimal('1E-8'), Decimal('1E+20')),
            app.ProviderSpend(Decimal('2E-8'), Decimal('2E+20')),
            NOW,
        )
        results = []
        for capitals in (0, 1):
            with patch.object(app, 'get_live_report', return_value=report), localcontext() as context:
                context.prec = 3
                context.capitals = capitals
                status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
            self.assertEqual(status, 0, errors)
            results.append(json.loads(output))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['providers'], {
            'openai': {'daily_usd': '0.00000001', 'month_to_date_usd': '100000000000000000000'},
            'xai': {'daily_usd': '0.00000002', 'month_to_date_usd': '200000000000000000000'},
        })
        self.assertEqual(results[0]['daily_total_usd'], '0.00000003')
        self.assertEqual(results[0]['month_to_date_total_usd'], '300000000000000000000')

    def test_smoke_creates_no_artifacts_and_invokes_no_delivery_functions(self) -> None:
        '''Even successful billing reads cannot generate PDFs, JSON files, markers or uploads.'''
        report = app.BillingReport(REPORT_DATE, app.ProviderSpend(Decimal('1'), Decimal('2')),
                                   app.ProviderSpend(Decimal('3'), Decimal('4')), NOW)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'get_live_report', return_value=report), \
             patch.object(app, 'create_pdf') as pdf, \
             patch.object(app, 'write_json_atomic') as write, \
             patch.object(app, 'AppsScriptPublisher') as script, \
             patch.object(app, 'DrivePublisher') as drive, \
             patch.object(app, 'send_email') as email, \
             patch.object(app, 'send_whatsapp') as whatsapp:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
            self.assertEqual(status, 0, errors)
            self.assertFalse(json.loads(output)['files_written'])
            self.assertFalse(json.loads(output)['delivery_performed'])
            for function in (pdf, write, script, drive, email, whatsapp):
                function.assert_not_called()

    def test_untrusted_provider_exception_is_never_echoed_or_traced(self) -> None:
        '''Unexpected failures receive a fixed code instead of secret-bearing exception text.'''
        private = 'mock-sensitive-value-that-must-not-appear'
        with patch.object(app, 'get_live_report', side_effect=RuntimeError(private)):
            status, output, errors = self.run_main(['--live', '--date', '2026-10-06'])
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertNotIn(private, errors)
        self.assertNotIn('Traceback', errors)


if __name__ == '__main__':
    unittest.main()
