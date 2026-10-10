'''Offline regression coverage for independent, secret-safe billing diagnostics.'''

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import requests

import app


SERVICE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'billing_diagnostics_helper', SERVICE_ROOT / 'scripts' / 'billing_smoke_test.py',
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError('Billing diagnostic helper could not be loaded')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

REPORT_DATE = date(2026, 10, 6)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
VALIDATION_URL = 'https://management-api.x.ai/auth/management-keys/validation'
XAI_BILLING_URL = 'https://management-api.x.ai/v1/billing/teams/team_unit/usage'
ENVIRONMENT = {
    'OPENAI_ADMIN_KEY': 'diagnostic-unit-openai-key',
    'XAI_MANAGEMENT_KEY': 'diagnostic-unit-xai-key',
    'XAI_TEAM_ID': 'team_unit',
    'OPENAI_PROJECT_IDS': '',
}
PRIVATE_MARKER = 'synthetic-private-details-must-not-appear'
CHECK_FIELDS = {
    'request_attempted', 'requests_attempted', 'http_status', 'http_statuses',
    'status', 'failure_stage', 'failure_code', 'error_category',
    'authentication_rejected', 'permission_rejected', 'invalid_request_rejected',
    'invalid_response_schema', 'response_completeness_failed', 'pagination_failed',
    'missing_utc_days', 'daily_usage_state', 'month_to_date_usage_state',
}


class FixedDatetime(datetime):
    '''Keep completed-UTC-day validation independent of the host clock.'''

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        '''Return a fixed observation instant in the requested timezone.'''
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ResponseStub:
    '''Represent HTTP results without connecting or exposing private mocked content.'''

    def __init__(self, body: Any, status_code: int = 200) -> None:
        '''Encode provider-shaped fixture bytes and retain resource state.'''
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.status_code = status_code
        self.headers = {'Content-Type': 'application/json', 'X-Synthetic-Private': PRIVATE_MARKER}
        self.closed = False

    @property
    def text(self) -> str:
        '''Expose mocked UTF-8 text for strict parsers.'''
        return self.content.decode('utf-8')

    def json(self, **kwargs: Any) -> Any:
        '''Honor Decimal and duplicate-key parsing options.'''
        return json.loads(self.content, **kwargs)

    def raise_for_status(self) -> None:
        '''Provide an intentionally unsafe exception for leakage assertions.'''
        if self.status_code >= 400:
            raise requests.HTTPError(PRIVATE_MARKER)

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        '''Support bounded mocked response streaming.'''
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self) -> None:
        '''Record redirected response cleanup.'''
        self.closed = True


def openai_body(*, zero: bool = False, days: tuple[int, ...] = tuple(range(1, 7)),
                project_id: str | None = None) -> dict[str, Any]:
    '''Create explicit full UTC-day buckets with recorded or confirmed-zero amounts.'''
    buckets = []
    for day in days:
        start = datetime(2026, 10, day, tzinfo=timezone.utc)
        item = {'object': 'organization.costs.result',
                'amount': {'value': '0' if zero else '0.1000000000000000001', 'currency': 'usd'},
                'line_item': PRIVATE_MARKER}
        if project_id is not None:
            item['project_id'] = project_id
        buckets.append({'object': 'bucket', 'start_time': int(start.timestamp()),
                        'end_time': int((start + timedelta(days=1)).timestamp()), 'results': [item]})
    return {'object': 'page', 'data': buckets, 'has_more': False}


def xai_body(*, zero: bool = False, days: tuple[int, ...] = tuple(range(1, 7))) -> dict[str, Any]:
    '''Create one documented dense USD time series across the requested UTC days.'''
    return {'limitReached': False, 'timeSeries': [{
        'groupLabels': [PRIVATE_MARKER],
        'dataPoints': [{'timestamp': f'2026-10-{day:02d}T00:00:00Z',
                        'values': ['0' if zero else '0.2000000000000000002']} for day in days],
    }]}


def ungrouped_zero_body(*, days: tuple[int, ...] = tuple(range(1, 7))) -> dict[str, Any]:
    '''Use explicit daily aggregate zeros, without inventing a description group.'''
    body = xai_body(zero=True, days=days)
    body['timeSeries'][0]['groupLabels'] = []
    return body


def validation_body() -> dict[str, Any]:
    '''Use the documented management-key schema instead of an invented valid flag.'''
    return {'apiKeyId': PRIVATE_MARKER, 'scope': 'SCOPE_TEAM', 'scopeId': 'team_unit',
            'teamId': 'team_unit', 'acls': [PRIVATE_MARKER]}


class BillingDiagnosticTests(unittest.TestCase):
    '''Independent provider evidence must never become misleading costs or secret output.'''

    def setUp(self) -> None:
        '''Install synthetic configuration and a fixed UTC clock only.'''
        environment = patch.dict(os.environ, ENVIRONMENT, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for module in (app, smoke):
            clock = patch.object(module, 'datetime', FixedDatetime)
            clock.start()
            self.addCleanup(clock.stop)

    def run_main(self, arguments: list[str]) -> tuple[int, dict[str, Any] | None, str, str]:
        '''Capture structured stdout separately from sanitized stderr.'''
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = smoke.main(arguments)
        raw_output, raw_errors = output.getvalue(), errors.getvalue()
        result = json.loads(raw_output) if raw_output else None
        for private in (*ENVIRONMENT.values(), PRIVATE_MARKER):
            if private:
                self.assertNotIn(private, raw_output + raw_errors)
        self.assertNotIn('Traceback', raw_output + raw_errors)
        return code, result, raw_output, raw_errors

    def run_mocked(self, replies: list[ResponseStub | Exception], *, provider: str = 'both') -> tuple[
        int, dict[str, Any], list[tuple[requests.Session, str, str, dict[str, Any]]],
    ]:
        '''Exercise actual provider parsers through the guarded session and fake transport.'''
        calls = []

        def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> ResponseStub:
            '''Record fixed read endpoints and security flags before interpreting replies.'''
            self.assertFalse(session.trust_env)
            self.assertIsNone(session.auth)
            self.assertEqual(len(session.cookies), 0)
            self.assertIs(kwargs.get('verify'), True)
            self.assertIs(kwargs.get('allow_redirects'), False)
            calls.append((session, method, url, kwargs))
            if not replies:
                raise AssertionError('Unexpected additional provider request')
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(requests.Session, 'request', autospec=True, side_effect=request):
            code, result, _output, _errors = self.run_main(
                ['--diagnostic', '--live', '--provider', provider, '--date', '2026-10-06'],
            )
        self.assertEqual(replies, [], 'Every expected read must be attempted independently')
        self.assertIsInstance(result, dict)
        assert result is not None
        for check in result['checks'].values():
            self.assertTrue(CHECK_FIELDS <= set(check))
            self.assertEqual(check['requests_attempted'], len(check['http_statuses']))
        self.assertNotIn('daily_total_usd', result)
        self.assertNotIn('month_to_date_total_usd', result)
        self.assertFalse(result['files_written'])
        self.assertFalse(result['delivery_performed'])
        return code, result, calls

    def test_offline_diagnostics_never_read_environment_or_start_requests(self) -> None:
        '''Every provider selection stays a credential-free plan until --live is explicit.'''
        original_get = os.environ.get

        def guarded_get(name: str, default: Any = None) -> Any:
            '''Permit argparse locale defaults but prohibit every billing configuration read.'''
            if name in ENVIRONMENT:
                raise AssertionError('Offline planning cannot inspect billing configuration')
            return original_get(name, default)

        for arguments in (['--diagnostic'], ['--diagnostic', '--provider', 'both'],
                          ['--diagnostic', '--provider', 'openai'], ['--diagnostic', '--provider', 'xai']):
            with self.subTest(arguments=arguments), \
                 patch.object(os.environ, 'get', side_effect=guarded_get), \
                 patch.object(smoke, 'ReadOnlyBillingSession') as session, \
                 patch.object(requests.Session, 'request') as transport:
                code, result, _output, _errors = self.run_main([*arguments, '--date', '2026-10-06'])
                session.assert_not_called()
                transport.assert_not_called()
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'prepared_offline')
            self.assertTrue(result['diagnostic'])
            self.assertFalse(result['network_requests'])
            self.assertFalse(result['files_written'])
            self.assertFalse(result['delivery_performed'])

    def test_provider_selection_requires_diagnostic_and_rejects_unsafe_values(self) -> None:
        '''Ordinary smoke behavior remains both-provider and invalid values cannot leak.'''
        for arguments in (['--provider', 'openai'], ['--live', '--provider', 'xai'],
                          ['--diagnostic', '--provider', PRIVATE_MARKER]):
            with patch.object(requests.Session, 'request') as transport:
                code, result, _output, _errors = self.run_main([*arguments, '--date', '2026-10-06'])
                transport.assert_not_called()
            self.assertEqual(code, 1)
            self.assertIsNone(result)

    def test_invalid_diagnostic_dates_emit_safe_global_preflight_without_configuration_reads(self) -> None:
        '''No credential lookup or request occurs when the billing day itself is invalid.'''
        original_get = os.environ.get

        def guarded_get(name: str, default: Any = None) -> Any:
            '''Reject billing preflight reads before a valid billing day exists.'''
            if name in ENVIRONMENT:
                raise AssertionError('Invalid dates cannot inspect billing configuration')
            return original_get(name, default)

        for report_day in ('2026-10-10', '1999-12-31', '2026-10-06T00:00:00Z', PRIVATE_MARKER):
            with self.subTest(report_day=report_day), \
                 patch.object(os.environ, 'get', side_effect=guarded_get), \
                 patch.object(requests.Session, 'request') as transport:
                code, result, _output, _errors = self.run_main([
                    '--diagnostic', '--live', '--date', report_day,
                ])
                transport.assert_not_called()
            self.assertEqual(code, 1)
            self.assertEqual(result['checks'], {})
            self.assertFalse(result['network_requests'])
            self.assertEqual(result['global_error']['failure_code'], 'invalid_report_date')
            self.assertEqual(result['global_error']['failure_stage'], 'preflight')

    def test_default_both_reads_have_independent_complete_evidence(self) -> None:
        '''Default live diagnostics make three fixed read calls with fresh stage sessions.'''
        replies = [ResponseStub(openai_body()), ResponseStub(validation_body()), ResponseStub(xai_body())]
        code, result, calls = self.run_mocked(replies)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'diagnostics_confirmed')
        self.assertEqual(set(result['checks']), {'openai_costs', 'xai_management_key_validation', 'xai_billing'})
        self.assertEqual([(method, url) for _session, method, url, _kwargs in calls], [
            ('GET', app.OPENAI_COSTS_URL), ('GET', VALIDATION_URL), ('POST', XAI_BILLING_URL),
        ])
        self.assertEqual(len({id(session) for session, _method, _url, _kwargs in calls}), 3)
        for name in ('openai_costs', 'xai_billing'):
            check = result['checks'][name]
            self.assertEqual(check['status'], 'costs_confirmed')
            self.assertEqual(check['http_statuses'], [200])
            self.assertEqual(check['daily_usage_state'], 'confirmed_recorded_cost')
            self.assertEqual(check['month_to_date_usage_state'], 'confirmed_recorded_cost')
        self.assertEqual(result['checks']['openai_costs']['daily_usd'], '0.1000000000000000001')
        self.assertEqual(result['checks']['xai_billing']['daily_usd'], '0.2000000000000000002')
        validation = result['checks']['xai_management_key_validation']
        self.assertEqual(validation['status'], 'key_confirmed')
        self.assertEqual(validation['key_scope'], 'team')
        self.assertEqual(validation['team_linkage'], 'match')

    def test_openai_401_does_not_prevent_xai_key_validation_or_billing(self) -> None:
        '''Authentication failure remains local to one provider instead of short-circuiting both.'''
        code, result, calls = self.run_mocked([
            ResponseStub({'error': PRIVATE_MARKER}, 401), ResponseStub(validation_body()), ResponseStub(xai_body()),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 3)
        openai = result['checks']['openai_costs']
        self.assertEqual(openai['error_category'], 'authentication_rejected')
        self.assertTrue(openai['authentication_rejected'])
        self.assertEqual(openai['http_status'], 401)
        self.assertEqual(openai['http_statuses'], [401])
        self.assertEqual(openai['daily_usage_state'], 'unknown')
        self.assertNotIn('daily_usd', openai)
        self.assertEqual(result['checks']['xai_management_key_validation']['status'], 'key_confirmed')
        self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')

    def test_xai_key_failure_still_attempts_validly_configured_billing(self) -> None:
        '''The validation endpoint cannot establish or deny historical billing permission.'''
        code, result, calls = self.run_mocked([
            ResponseStub({'error': PRIVATE_MARKER}, 401), ResponseStub(xai_body()),
        ], provider='xai')
        self.assertEqual(code, 1)
        self.assertEqual([(method, url) for _session, method, url, _kwargs in calls],
                         [('GET', VALIDATION_URL), ('POST', XAI_BILLING_URL)])
        self.assertTrue(result['checks']['xai_management_key_validation']['authentication_rejected'])
        self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')

    def test_key_confirmation_does_not_claim_historical_billing_access(self) -> None:
        '''A valid management key plus denied billing stays a failed diagnostic with no totals.'''
        code, result, _calls = self.run_mocked([
            ResponseStub(validation_body()), ResponseStub({'error': PRIVATE_MARKER}, 403),
        ], provider='xai')
        self.assertEqual(code, 1)
        self.assertEqual(result['checks']['xai_management_key_validation']['status'], 'key_confirmed')
        billing = result['checks']['xai_billing']
        self.assertEqual(billing['error_category'], 'permission_rejected')
        self.assertTrue(billing['permission_rejected'])
        self.assertEqual(billing['daily_usage_state'], 'unknown')
        self.assertNotIn('daily_usd', billing)

    def test_openai_only_does_not_read_or_require_xai_configuration(self) -> None:
        '''Provider isolation includes configuration, not only the HTTP request list.'''
        original_get = os.environ.get

        def guarded_get(name: str, default: Any = None) -> Any:
            if name in ('XAI_MANAGEMENT_KEY', 'XAI_TEAM_ID'):
                raise AssertionError('OpenAI-only diagnostics cannot inspect xAI configuration')
            return original_get(name, default)

        with patch.object(os.environ, 'get', side_effect=guarded_get):
            code, result, calls = self.run_mocked([ResponseStub(openai_body())], provider='openai')
        self.assertEqual(code, 0)
        self.assertEqual(set(result['checks']), {'openai_costs'})
        self.assertEqual(len(calls), 1)

    def test_xai_only_does_not_read_or_require_openai_configuration(self) -> None:
        '''xAI-only diagnostics remain possible without an OpenAI key or project scope.'''
        original_get = os.environ.get

        def guarded_get(name: str, default: Any = None) -> Any:
            if name in ('OPENAI_ADMIN_KEY', 'OPENAI_PROJECT_IDS'):
                raise AssertionError('xAI-only diagnostics cannot inspect OpenAI configuration')
            return original_get(name, default)

        with patch.object(os.environ, 'get', side_effect=guarded_get):
            code, result, _calls = self.run_mocked(
                [ResponseStub(validation_body()), ResponseStub(xai_body())], provider='xai',
            )
        self.assertEqual(code, 0)
        self.assertEqual(set(result['checks']), {'xai_management_key_validation', 'xai_billing'})

    def test_openai_diagnostics_retain_exact_selected_project_wire_scope(self) -> None:
        '''Diagnostics must use the same verified project filter as the daily accounting job.'''
        with patch.dict(os.environ, {'OPENAI_PROJECT_IDS': 'proj_diagA,proj_diagA'}):
            code, result, calls = self.run_mocked([
                ResponseStub(openai_body(project_id='proj_diagA')),
            ], provider='openai')
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][3]['params']['project_ids[]'], ['proj_diagA'])
        self.assertEqual(calls[0][3]['params']['group_by[]'], ['line_item', 'project_id'])
        check = result['checks']['openai_costs']
        self.assertEqual(check['openai_scope'], 'selected_projects')
        self.assertEqual(check['configured_project_count'], 1)

    def test_missing_openai_key_cannot_block_valid_xai_attempts(self) -> None:
        '''Provider-local preflight failures are reported without making that provider's request.'''
        with patch.dict(os.environ, {'OPENAI_ADMIN_KEY': ''}):
            code, result, calls = self.run_mocked([ResponseStub(validation_body()), ResponseStub(xai_body())])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        failed = result['checks']['openai_costs']
        self.assertFalse(failed['request_attempted'])
        self.assertEqual(failed['requests_attempted'], 0)
        self.assertEqual(failed['http_statuses'], [])
        self.assertEqual(failed['failure_stage'], 'preflight')
        self.assertEqual(failed['failure_code'], 'configuration_missing')
        self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')

    def test_missing_team_still_allows_management_key_validation(self) -> None:
        '''Key validation uses only the management key; billing requires the team configuration.'''
        with patch.dict(os.environ, {'XAI_TEAM_ID': ''}):
            code, result, calls = self.run_mocked([ResponseStub(validation_body())], provider='xai')
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['checks']['xai_management_key_validation']['status'], 'key_confirmed')
        self.assertEqual(result['checks']['xai_management_key_validation']['team_linkage'], 'not_configured')
        self.assertFalse(result['checks']['xai_billing']['request_attempted'])
        self.assertEqual(result['checks']['xai_billing']['failure_stage'], 'preflight')

    def test_missing_xai_key_does_not_prevent_openai_cost_diagnostic(self) -> None:
        '''Both xAI stages stop before requests when their key is absent, independently of OpenAI.'''
        with patch.dict(os.environ, {'XAI_MANAGEMENT_KEY': ''}):
            code, result, calls = self.run_mocked([ResponseStub(openai_body())])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['checks']['openai_costs']['status'], 'costs_confirmed')
        for name in ('xai_management_key_validation', 'xai_billing'):
            self.assertFalse(result['checks'][name]['request_attempted'])
            self.assertEqual(result['checks'][name]['failure_code'], 'configuration_missing')

    def test_confirmed_zeros_require_complete_accounting_coverage(self) -> None:
        '''Recorded dense zero values are distinguished from absent provider accounting data.'''
        code, result, _calls = self.run_mocked([
            ResponseStub(openai_body(zero=True)), ResponseStub(validation_body()), ResponseStub(xai_body(zero=True)),
        ])
        self.assertEqual(code, 0)
        for name in ('openai_costs', 'xai_billing'):
            check = result['checks'][name]
            self.assertEqual(check['daily_usage_state'], 'confirmed_zero')
            self.assertEqual(check['month_to_date_usage_state'], 'confirmed_zero')
            self.assertEqual(check['daily_usd'], '0')
            self.assertEqual(check['month_to_date_usd'], '0')
        self.assertEqual(result['checks']['xai_billing']['accounting_evidence'], 'explicit_daily_cost_records')
        self.assertFalse(result['checks']['xai_billing']['zero_confirmation_attempted'])
        self.assertFalse(result['checks']['xai_billing']['zero_confirmation_confirmed'])

    def test_two_valid_empty_xai_datasets_report_no_recorded_usage_without_zero_reconciliation(self) -> None:
        '''Absent records have a distinct recheckable state instead of confirmed-zero accounting.'''
        code, result, _calls = self.run_mocked([
            ResponseStub(validation_body()), ResponseStub({'limitReached': False, 'timeSeries': []}),
            ResponseStub({'limitReached': False, 'timeSeries': []}),
        ], provider='xai')
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'diagnostics_recorded_usage_absent')
        billing = result['checks']['xai_billing']
        self.assertEqual(billing['status'], 'recorded_usage_absent')
        self.assertEqual(billing['accounting_state'], 'NO_RECORDED_USAGE')
        self.assertEqual(billing['accounting_evidence'], 'grouped_and_ungrouped_empty')
        self.assertEqual(billing['coverage'], 'valid_empty_datasets')
        self.assertEqual(billing['reconciliation_status'], 'not_reconciled')
        self.assertTrue(billing['allow_recheck'])
        self.assertFalse(billing['response_completeness_failed'])
        self.assertEqual(billing['missing_utc_days'], [])
        self.assertEqual(billing['http_status'], 200)
        self.assertEqual(billing['http_statuses'], [200, 200])
        self.assertTrue(billing['zero_confirmation_attempted'])
        self.assertFalse(billing['zero_confirmation_confirmed'])
        self.assertEqual(billing['daily_usage_state'], 'no_recorded_usage')
        self.assertEqual(billing['month_to_date_usage_state'], 'no_recorded_usage')
        self.assertEqual(billing['daily_usd'], '0')
        self.assertEqual(billing['month_to_date_usd'], '0')
        self.assertEqual(len(billing['response_schemas']), 2)
        for mode, schema in zip(('description_grouped', 'ungrouped'), billing['response_schemas']):
            self.assertEqual(schema, {
                'query_mode': mode, 'json_status': 'valid_object', 'limit_reached_type': 'boolean',
                'limit_reached': False, 'time_series_type': 'array', 'time_series_count': 0,
                'data_point_count': 0, 'usd_value_count': 0, 'invalid_series_count': 0,
                'invalid_point_count': 0, 'unexpected_top_level_field_count': 0, 'metadata_complete': True,
            })

    def test_fresh_read_can_transition_from_no_recorded_usage_to_later_recorded_spending(self) -> None:
        '''The no-record state never overrides newly returned provider accounting records.'''
        first_code, first, first_calls = self.run_mocked([
            ResponseStub(validation_body()), ResponseStub({'limitReached': False, 'timeSeries': []}),
            ResponseStub({'limitReached': False, 'timeSeries': []}),
        ], provider='xai')
        later_code, later, later_calls = self.run_mocked([
            ResponseStub(validation_body()), ResponseStub(xai_body()),
        ], provider='xai')
        self.assertEqual(first_code, 0)
        self.assertEqual(first['checks']['xai_billing']['accounting_state'], 'NO_RECORDED_USAGE')
        self.assertEqual(len(first_calls), 3)
        self.assertEqual(later_code, 0)
        self.assertEqual(len(later_calls), 2)
        observed = later['checks']['xai_billing']
        self.assertEqual(observed['accounting_state'], 'RECORDED_SPENDING')
        self.assertEqual(observed['daily_usage_state'], 'confirmed_recorded_cost')
        self.assertEqual(observed['daily_usd'], '0.2000000000000000002')
        self.assertEqual(observed['month_to_date_usd'], '1.2000000000000000012')
        self.assertEqual(observed['accounting_evidence'], 'explicit_daily_cost_records')
        self.assertFalse(observed['zero_confirmation_attempted'])
        self.assertEqual(later['status'], 'diagnostics_confirmed')

    def test_openai_reader_cannot_claim_xai_inactive_accounting_metadata(self) -> None:
        '''Provider-specific absence evidence cannot be applied to another billing provider.'''
        invalid = app.ProviderSpend(
            Decimal('0'), Decimal('0'), {}, accounting_state='NO_RECORDED_USAGE',
            accounting_evidence='grouped_and_ungrouped_empty',
            reconciliation_status='not_reconciled', allow_recheck=True,
        )
        with patch.object(app, 'fetch_openai_costs', return_value=invalid):
            code, result, calls = self.run_mocked([], provider='openai')
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        check = result['checks']['openai_costs']
        self.assertEqual(check['accounting_state'], 'UNKNOWN')
        self.assertEqual(check['failure_code'], 'response_schema_invalid')
        self.assertTrue(check['invalid_response_schema'])
        self.assertNotIn('daily_usd', check)
        self.assertNotIn('month_to_date_usd', check)

    def test_unknown_fields_in_empty_datasets_remain_failed_with_private_names_hidden(self) -> None:
        '''An error-shaped or ambiguous empty result cannot establish an inactive snapshot.'''
        empty = {'limitReached': False, 'timeSeries': []}
        unknown = {**empty, PRIVATE_MARKER: {'private_nested_value': PRIVATE_MARKER}}
        for first_unknown in (True, False):
            replies = [ResponseStub(validation_body()), ResponseStub(unknown if first_unknown else empty)]
            if not first_unknown:
                replies.append(ResponseStub(unknown))
            with self.subTest(first_unknown=first_unknown):
                code, result, calls = self.run_mocked(replies, provider='xai')
                self.assertEqual(code, 1)
                billing = result['checks']['xai_billing']
                self.assertEqual(billing['accounting_state'], 'UNKNOWN')
                self.assertEqual(billing['daily_usage_state'], 'unknown')
                self.assertNotIn('daily_usd', billing)
                self.assertEqual(len(calls), 2 if first_unknown else 3)
                self.assertEqual(billing['response_schemas'][-1]['unexpected_top_level_field_count'], 1)

    def test_schema_telemetry_contains_only_fixed_presence_types_and_counts(self) -> None:
        '''Unknown field names, labels, data values and headers never enter schema diagnostics.'''
        body = xai_body()
        body[PRIVATE_MARKER] = {'metadata_id': PRIVATE_MARKER}
        schema = smoke.schema_metadata(ResponseStub(body), 'description_grouped')
        self.assertEqual(set(schema), {
            'query_mode', 'json_status', 'limit_reached_type', 'limit_reached', 'time_series_type',
            'time_series_count', 'data_point_count', 'usd_value_count', 'invalid_series_count',
            'invalid_point_count', 'unexpected_top_level_field_count', 'metadata_complete',
        })
        self.assertEqual(schema['json_status'], 'valid_object')
        self.assertEqual(schema['query_mode'], 'description_grouped')
        self.assertEqual(schema['time_series_count'], 1)
        self.assertEqual(schema['data_point_count'], 6)
        self.assertEqual(schema['usd_value_count'], 6)
        self.assertEqual(schema['unexpected_top_level_field_count'], 1)
        self.assertTrue(schema['metadata_complete'])
        serialized = json.dumps(schema)
        for private in (PRIVATE_MARKER, '0.2000000000000000002', 'metadata_id', 'X-Synthetic-Private'):
            self.assertNotIn(private, serialized)

    def test_empty_points_are_not_no_recorded_usage_and_schema_counts_explain_coverage(self) -> None:
        '''A nonempty series with absent daily observations remains incomplete rather than inactive.'''
        body = xai_body(days=())
        code, result, _calls = self.run_mocked([
            ResponseStub(validation_body()), ResponseStub(body),
        ], provider='xai')
        self.assertEqual(code, 1)
        billing = result['checks']['xai_billing']
        self.assertEqual(billing['accounting_state'], 'UNKNOWN')
        self.assertTrue(billing['response_completeness_failed'])
        self.assertEqual(billing['missing_utc_days'], [f'2026-10-{day:02d}' for day in range(1, 7)])
        self.assertEqual(billing['response_schemas'][0]['time_series_count'], 1)
        self.assertEqual(billing['response_schemas'][0]['data_point_count'], 0)
        self.assertEqual(billing['response_schemas'][0]['usd_value_count'], 0)
        self.assertNotIn('daily_usd', billing)

    def test_invalid_json_denied_http_and_timeout_schema_slots_are_safe_and_unavailable(self) -> None:
        '''Every attempt retains an explanatory schema slot without parsing rejected response bodies.'''
        for reply, expected in (
            (ResponseStub(b'not-json-' + PRIVATE_MARKER.encode()), 'invalid_json'),
            (ResponseStub({'private': PRIVATE_MARKER}, 403), 'non_success_http'),
            (requests.Timeout(PRIVATE_MARKER), 'no_http_response'),
        ):
            with self.subTest(expected=expected):
                code, result, _calls = self.run_mocked([
                    ResponseStub(validation_body()), ResponseStub({'limitReached': False, 'timeSeries': []}),
                    reply,
                ], provider='xai')
                self.assertEqual(code, 1)
                billing = result['checks']['xai_billing']
                self.assertEqual(billing['accounting_state'], 'UNKNOWN')
                self.assertEqual(len(billing['response_schemas']), 2)
                schema = billing['response_schemas'][1]
                self.assertEqual(schema['query_mode'], 'ungrouped')
                self.assertEqual(schema['json_status'], expected)
                self.assertIsNone(schema['time_series_count'])
                self.assertIsNone(schema['data_point_count'])
                self.assertIsNone(schema['usd_value_count'])
                self.assertFalse(schema['metadata_complete'])

    def test_schema_inspection_limit_does_not_disclose_or_claim_complete_metadata(self) -> None:
        '''Oversized inspection work is bounded and marked incomplete instead of echoing content.'''
        with patch.object(app, 'MAX_BILLING_ROWS', 1):
            schema = smoke.schema_metadata(ResponseStub(xai_body()), 'description_grouped')
        self.assertFalse(schema['metadata_complete'])
        self.assertNotIn(PRIVATE_MARKER, json.dumps(schema))

    def test_empty_grouped_xai_is_zero_only_after_complete_same_period_aggregate_confirmation(self) -> None:
        '''A second read confirms every daily USD zero with the original team, scope and period.'''
        for omit_labels in (False, True):
            aggregate = ungrouped_zero_body()
            if omit_labels:
                aggregate['timeSeries'][0].pop('groupLabels')
            with self.subTest(omit_labels=omit_labels):
                code, result, calls = self.run_mocked([
                    ResponseStub(validation_body()), ResponseStub({'limitReached': False, 'timeSeries': []}),
                    ResponseStub(aggregate),
                ], provider='xai')
                self.assertEqual(code, 0)
                billing = result['checks']['xai_billing']
                self.assertEqual(billing['http_statuses'], [200, 200])
                self.assertEqual(billing['requests_attempted'], 2)
                self.assertEqual(billing['daily_usd'], '0')
                self.assertEqual(billing['month_to_date_usd'], '0')
                self.assertEqual(billing['daily_usage_state'], 'confirmed_zero')
                self.assertEqual(billing['month_to_date_usage_state'], 'confirmed_zero')
                self.assertEqual(billing['accounting_evidence'], 'ungrouped_dense_zero_confirmation')
                self.assertEqual(billing['accounting_state'], 'RECORDED_SPENDING')
                self.assertTrue(billing['zero_confirmation_attempted'])
                self.assertTrue(billing['zero_confirmation_confirmed'])
                self.assertEqual([(method, url) for _session, method, url, _kwargs in calls], [
                    ('GET', VALIDATION_URL), ('POST', XAI_BILLING_URL), ('POST', XAI_BILLING_URL),
                ])
                grouped = calls[1][3]['json']['analyticsRequest']
                ungrouped = calls[2][3]['json']['analyticsRequest']
                self.assertEqual(grouped['groupBy'], ['description'])
                self.assertEqual(ungrouped, {**grouped, 'groupBy': []})
                self.assertEqual(ungrouped['values'], [{'name': 'usd', 'aggregation': 'AGGREGATION_SUM'}])
                self.assertEqual(ungrouped['timeUnit'], 'TIME_UNIT_DAY')
                self.assertEqual(ungrouped['filters'], [])
                self.assertEqual(ungrouped['timeRange']['endTime'], '2026-10-07 00:00:00')

    def test_xai_zero_confirmation_stays_independent_of_openai_authentication_failure(self) -> None:
        '''One denied provider cannot prevent a valid xAI dense-zero confirmation.'''
        code, result, calls = self.run_mocked([
            ResponseStub({'error': PRIVATE_MARKER}, 401), ResponseStub(validation_body()),
            ResponseStub({'limitReached': False, 'timeSeries': []}), ResponseStub(ungrouped_zero_body()),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 4)
        self.assertTrue(result['checks']['openai_costs']['authentication_rejected'])
        self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')
        self.assertTrue(result['checks']['xai_billing']['zero_confirmation_confirmed'])

    def test_nonzero_aggregate_or_refund_net_zero_cannot_confirm_empty_grouped_costs(self) -> None:
        '''Contradictory recorded amounts cannot be rounded or netted into zero evidence.'''
        for amounts in (['1E-100', '0', '0', '0', '0', '0'], ['1', '-1', '0', '0', '0', '0']):
            aggregate = ungrouped_zero_body()
            for point, value in zip(aggregate['timeSeries'][0]['dataPoints'], amounts):
                point['values'] = [value]
            with self.subTest(amounts=amounts):
                code, result, _calls = self.run_mocked([
                    ResponseStub(validation_body()), ResponseStub({'limitReached': False, 'timeSeries': []}),
                    ResponseStub(aggregate),
                ], provider='xai')
                self.assertEqual(code, 1)
                billing = result['checks']['xai_billing']
                self.assertEqual(billing['failure_code'], 'aggregation_mismatch')
                self.assertEqual(billing['failure_stage'], 'coverage')
                self.assertTrue(billing['response_completeness_failed'])
                self.assertTrue(billing['zero_confirmation_attempted'])
                self.assertFalse(billing['zero_confirmation_confirmed'])
                self.assertEqual(billing['daily_usage_state'], 'unknown')
                self.assertNotIn('daily_usd', billing)

    def test_partial_or_malformed_ungrouped_confirmation_remains_unknown(self) -> None:
        '''Missing dates, ambiguous series and invalid metric values never establish zero costs.'''
        missing_day = ungrouped_zero_body(days=(1, 2, 3, 4, 5))
        duplicate_series = ungrouped_zero_body()
        duplicate_series['timeSeries'].append(ungrouped_zero_body()['timeSeries'][0])
        wrong_labels = ungrouped_zero_body()
        wrong_labels['timeSeries'][0]['groupLabels'] = [PRIVATE_MARKER]
        malformed_metric = ungrouped_zero_body()
        malformed_metric['timeSeries'][0]['dataPoints'][0]['values'] = []
        truncated = {'limitReached': True, 'timeSeries': []}
        for aggregate in (missing_day, duplicate_series, wrong_labels, malformed_metric, truncated):
            with self.subTest(series_count=len(aggregate['timeSeries'])):
                code, result, calls = self.run_mocked([
                    ResponseStub(validation_body()), ResponseStub({'limitReached': False, 'timeSeries': []}),
                    ResponseStub(aggregate),
                ], provider='xai')
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 3)
                billing = result['checks']['xai_billing']
                self.assertEqual(billing['http_statuses'], [200, 200])
                self.assertTrue(billing['zero_confirmation_attempted'])
                self.assertFalse(billing['zero_confirmation_confirmed'])
                self.assertEqual(billing['daily_usage_state'], 'unknown')
                self.assertNotIn('daily_usd', billing)
                if aggregate is missing_day:
                    self.assertEqual(billing['missing_utc_days'], ['2026-10-06'])

    def test_zero_confirmation_http_and_transport_failures_preserve_second_attempt_evidence(self) -> None:
        '''A failed aggregate read keeps provider rejection distinct from accounting confirmation.'''
        for response, status, category in (
            (ResponseStub({'private': PRIVATE_MARKER}, 400), 400, 'invalid_request'),
            (ResponseStub({'private': PRIVATE_MARKER}, 401), 401, 'authentication_rejected'),
            (ResponseStub({'private': PRIVATE_MARKER}, 403), 403, 'permission_rejected'),
            (ResponseStub({'private': PRIVATE_MARKER}, 503), 503, 'provider_error'),
            (requests.Timeout(PRIVATE_MARKER), None, 'timeout'),
        ):
            with self.subTest(category=category):
                code, result, _calls = self.run_mocked([
                    ResponseStub(validation_body()), ResponseStub({'limitReached': False, 'timeSeries': []}),
                    response,
                ], provider='xai')
                self.assertEqual(code, 1)
                billing = result['checks']['xai_billing']
                self.assertEqual(billing['requests_attempted'], 2)
                self.assertEqual(billing['http_statuses'], [200, status])
                self.assertEqual(billing['http_status'], status)
                self.assertEqual(billing['error_category'], category)
                self.assertTrue(billing['zero_confirmation_attempted'])
                self.assertFalse(billing['zero_confirmation_confirmed'])
                self.assertEqual(billing['daily_usage_state'], 'unknown')
                self.assertNotIn('daily_usd', billing)

    def test_partial_truncated_or_malformed_grouped_data_does_not_trigger_zero_confirmation(self) -> None:
        '''The fallback applies only to a valid empty grouped result, never to incomplete data.'''
        for grouped in (
            xai_body(days=(1, 2, 3, 4, 5)), {'limitReached': True, 'timeSeries': []},
            {'limitReached': False, 'timeSeries': 'invalid'}, {'timeSeries': []},
        ):
            with self.subTest(response_type=type(grouped.get('timeSeries')).__name__):
                code, result, calls = self.run_mocked([
                    ResponseStub(validation_body()), ResponseStub(grouped),
                ], provider='xai')
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 2)
                billing = result['checks']['xai_billing']
                self.assertEqual(billing['requests_attempted'], 1)
                self.assertFalse(billing['zero_confirmation_attempted'])
                self.assertFalse(billing['zero_confirmation_confirmed'])
                self.assertEqual(billing['daily_usage_state'], 'unknown')

    def test_missing_openai_bucket_dates_are_reported_without_partial_totals(self) -> None:
        '''A successful costs HTTP status still fails when complete UTC-day coverage is missing.'''
        code, result, _calls = self.run_mocked([
            ResponseStub(openai_body(days=(1, 3, 4, 5))),
        ], provider='openai')
        self.assertEqual(code, 1)
        check = result['checks']['openai_costs']
        self.assertEqual(check['failure_code'], 'incomplete_coverage')
        self.assertEqual(check['missing_utc_days'], ['2026-10-02', '2026-10-06'])
        self.assertTrue(check['response_completeness_failed'])
        self.assertEqual(check['http_statuses'], [200])
        self.assertNotIn('daily_usd', check)

    def test_malformed_second_cost_page_preserves_both_http_attempts(self) -> None:
        '''Pagination diagnostics show every attempted page without treating HTTP 200 as valid data.'''
        first = openai_body(days=(1, 2, 3))
        first.update(has_more=True, next_page=PRIVATE_MARKER)
        second = openai_body(days=(4, 5, 6))
        second['has_more'] = 'not-a-boolean'
        code, result, calls = self.run_mocked([ResponseStub(first), ResponseStub(second)], provider='openai')
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        check = result['checks']['openai_costs']
        self.assertEqual(check['requests_attempted'], 2)
        self.assertEqual(check['http_statuses'], [200, 200])
        self.assertEqual(check['failure_code'], 'pagination_invalid')
        self.assertTrue(check['pagination_failed'])
        self.assertNotIn('daily_usd', check)

    def test_http_failure_categories_distinguish_auth_permission_and_invalid_request(self) -> None:
        '''Safe HTTP evidence gives actionable categories without exposing provider bodies.'''
        for http_status, category in ((400, 'invalid_request'), (401, 'authentication_rejected'),
                                      (403, 'permission_rejected'), (404, 'endpoint_or_scope_not_found'),
                                      (422, 'invalid_request'), (429, 'rate_limited'), (503, 'provider_error')):
            with self.subTest(http_status=http_status):
                code, result, _calls = self.run_mocked([
                    ResponseStub({'body': PRIVATE_MARKER}, http_status),
                ], provider='openai')
                self.assertEqual(code, 1)
                check = result['checks']['openai_costs']
                self.assertEqual(check['http_status'], http_status)
                self.assertEqual(check['error_category'], category)
                self.assertEqual(check['invalid_request_rejected'], http_status in (400, 422))
                self.assertEqual(check['authentication_rejected'], http_status == 401)
                self.assertEqual(check['permission_rejected'], http_status == 403)

    def test_validation_requires_documented_typed_schema_without_valid_flag(self) -> None:
        '''An invented valid:true flag is insufficient; malformed metadata never proves acceptance.'''
        malformed = [
            {'valid': True}, {'apiKeyId': ''}, {'apiKeyId': 1}, {'apiKeyId': 'a' * 257},
            {'apiKeyId': ' '}, {'apiKeyId': 'unit\ncontrol'}, {'apiKeyId': 'unit\u00e9'},
            {**validation_body(), 'scope': 'team'}, {**validation_body(), 'scopeId': False},
            {**validation_body(), 'teamId': {}}, {**validation_body(), 'acls': 'all'},
            {**validation_body(), 'acls': [1]},
        ]
        for body in malformed:
            with self.subTest(field_types={name: type(value).__name__ for name, value in body.items()}):
                code, result, calls = self.run_mocked([
                    ResponseStub(body), ResponseStub(xai_body()),
                ], provider='xai')
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 2)
                check = result['checks']['xai_management_key_validation']
                self.assertEqual(check['status'], 'failed')
                self.assertTrue(check['invalid_response_schema'])
                self.assertEqual(check['http_status'], 200)
                self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')

    def test_key_scope_and_team_linkage_output_contains_only_safe_enums(self) -> None:
        '''Diagnostic metadata excludes key IDs, scope IDs, names and ACL values.'''
        for scope, scope_id, expected_scope, linkage in (
            ('SCOPE_TEAM', 'other_unit_team', 'team', 'mismatch'),
            ('SCOPE_ORGANIZATION', PRIVATE_MARKER, 'organization', 'unknown'),
            ('SCOPE_UNSPECIFIED', PRIVATE_MARKER, 'unspecified', 'unknown'),
        ):
            body = {'apiKeyId': PRIVATE_MARKER, 'scope': scope, 'scopeId': scope_id, 'acls': [PRIVATE_MARKER]}
            with self.subTest(scope=scope):
                _code, result, _calls = self.run_mocked([
                    ResponseStub(body), ResponseStub(xai_body()),
                ], provider='xai')
                check = result['checks']['xai_management_key_validation']
                self.assertEqual(check['key_scope'], expected_scope)
                self.assertEqual(check['team_linkage'], linkage)
                if linkage == 'mismatch':
                    self.assertEqual(check['status'], 'failed')
                    self.assertTrue(check['key_accepted'])
                    self.assertEqual(check['failure_code'], 'scope_mismatch')
                    self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')
                for private_field in ('apiKeyId', 'scopeId', 'teamId', 'acls'):
                    self.assertNotIn(private_field, check)

    def test_malformed_or_oversized_validation_json_fails_without_blocking_billing(self) -> None:
        '''HTTP 200 is insufficient for malformed, ambiguous or oversized key metadata.'''
        for body in (b'not-json-' + PRIVATE_MARKER.encode(),
                     b'{"apiKeyId":"one","apiKeyId":"two"}',
                     {'apiKeyId': 'unit', 'private': PRIVATE_MARKER * 3000}):
            with self.subTest(body_type=type(body).__name__):
                code, result, calls = self.run_mocked([
                    ResponseStub(body), ResponseStub(xai_body()),
                ], provider='xai')
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 2)
                self.assertTrue(result['checks']['xai_management_key_validation']['invalid_response_schema'])
                self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')

    def test_unknown_mutated_error_attributes_are_not_serialized(self) -> None:
        '''Safe output is revalidated instead of trusting mutable exception metadata.'''
        error = app.ReporterError(PRIVATE_MARKER)
        error.code = PRIVATE_MARKER
        error.failure_stage = PRIVATE_MARKER
        error.details = {'private': PRIVATE_MARKER,
                         'missing_utc_days': [PRIVATE_MARKER, '2026-10-02', '2026-99-99']}
        with patch.object(app, 'fetch_openai_costs', side_effect=error):
            code, result, calls = self.run_mocked([], provider='openai')
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        check = result['checks']['openai_costs']
        self.assertEqual(check['failure_code'], 'internal_error')
        self.assertEqual(check['failure_stage'], 'unknown')
        self.assertEqual(check['missing_utc_days'], ['2026-10-02'])

    def test_unexpected_top_level_failure_keeps_request_certainty_unknown_and_text_private(self) -> None:
        '''A lost diagnostic context cannot truthfully claim that no dispatch took place.'''
        with patch.object(smoke, 'diagnose_billing', side_effect=RuntimeError(PRIVATE_MARKER)), \
             patch.object(requests.Session, 'request') as transport:
            code, result, _output, _errors = self.run_main([
                '--diagnostic', '--live', '--date', '2026-10-06',
            ])
            transport.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIsNone(result['network_requests'])
        self.assertIsNone(result['global_error']['request_attempted'])
        self.assertIsNone(result['global_error']['requests_attempted'])
        self.assertEqual(result['global_error']['failure_code'], 'internal_error')
        self.assertEqual(result['global_error']['failure_stage'], 'unknown')

    def test_network_failure_records_attempt_without_http_status_or_exception_text(self) -> None:
        '''Transport exceptions remain distinguishable from preflight failure and provider rejection.'''
        code, result, _calls = self.run_mocked([
            requests.ConnectionError(PRIVATE_MARKER), ResponseStub(validation_body()), ResponseStub(xai_body()),
        ])
        self.assertEqual(code, 1)
        check = result['checks']['openai_costs']
        self.assertTrue(check['request_attempted'])
        self.assertEqual(check['requests_attempted'], 1)
        self.assertEqual(check['http_statuses'], [None])
        self.assertIsNone(check['http_status'])
        self.assertEqual(check['failure_stage'], 'network')
        self.assertEqual(check['failure_code'], 'network_error')
        self.assertEqual(result['checks']['xai_billing']['status'], 'costs_confirmed')

    def test_second_page_timeout_preserves_each_attempt_including_no_http_response(self) -> None:
        '''A later failed dispatch cannot inherit the first page's HTTP 200 as its own status.'''
        first = openai_body(days=(1, 2, 3))
        first.update(has_more=True, next_page=PRIVATE_MARKER)
        code, result, _calls = self.run_mocked([
            ResponseStub(first), requests.Timeout(PRIVATE_MARKER),
        ], provider='openai')
        self.assertEqual(code, 1)
        check = result['checks']['openai_costs']
        self.assertEqual(check['requests_attempted'], 2)
        self.assertEqual(check['http_statuses'], [200, None])
        self.assertIsNone(check['http_status'])
        self.assertEqual(check['error_category'], 'timeout')
        self.assertEqual(check['failure_stage'], 'network')
        self.assertNotIn('daily_usd', check)

    def test_redirect_rejection_preserves_status_and_never_follows_location(self) -> None:
        '''The recorder observes HTTP 302 while preventing credential forwarding.'''
        reply = ResponseStub({'body': PRIVATE_MARKER}, 302)
        reply.headers['Location'] = 'https://attacker.invalid/' + PRIVATE_MARKER
        code, result, calls = self.run_mocked([reply], provider='openai')
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(reply.closed)
        self.assertEqual(result['checks']['openai_costs']['error_category'], 'redirect_rejected')
        self.assertEqual(result['checks']['openai_costs']['http_status'], 302)

    def test_diagnostic_transport_rejects_writes_and_near_match_endpoints(self) -> None:
        '''Adding validation never permits other auth endpoints, scopes or provider mutations.'''
        with smoke.ReadOnlyBillingSession('team_unit', diagnostic=True, provider='xai') as session, \
             patch.object(requests.Session, 'request') as transport:
            for method, url in (
                ('POST', VALIDATION_URL), ('DELETE', VALIDATION_URL),
                ('GET', VALIDATION_URL + '?private=' + PRIVATE_MARKER),
                ('GET', 'https://management-api.x.ai/auth/management-keys'),
                ('POST', XAI_BILLING_URL.replace('team_unit', 'other_unit_team')),
                ('GET', app.OPENAI_COSTS_URL),
            ):
                with self.subTest(method=method, endpoint=url.split('?')[0]), self.assertRaises(smoke.BillingSmokeError):
                    session.request(method, url)
            transport.assert_not_called()

    def test_diagnostics_write_no_files_and_invoke_no_reporting_or_delivery(self) -> None:
        '''Independent successes stay stdout-only instead of producing partial report snapshots.'''
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'create_pdf') as pdf, patch.object(app, 'write_json_atomic') as write, \
             patch.object(app, 'AppsScriptPublisher') as script, patch.object(app, 'DrivePublisher') as drive, \
             patch.object(app, 'send_email') as email, patch.object(app, 'send_whatsapp') as whatsapp:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                code, _result, _calls = self.run_mocked([
                    ResponseStub(openai_body()), ResponseStub(validation_body()), ResponseStub(xai_body()),
                ])
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
            self.assertEqual(code, 0)
            for function in (pdf, write, script, drive, email, whatsapp):
                function.assert_not_called()


if __name__ == '__main__':
    unittest.main()
