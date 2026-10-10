'''Inspect actual provider-recorded costs without writing or delivering reports.'''

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


XAI_MANAGEMENT_KEY_VALIDATION_URL = f'{app.XAI_BASE_URL}/auth/management-keys/validation'
MAX_KEY_VALIDATION_BYTES = 64 * 1024
SAFE_ERROR_CODES = frozenset({
    'internal_error', 'configuration_missing', 'invalid_config', 'invalid_report_date',
    'http_error', 'network_error', 'invalid_json', 'response_schema_invalid',
    'incomplete_coverage', 'pagination_invalid', 'pagination_limit', 'response_truncated',
    'no_accounting_data', 'invalid_usd_amount', 'unsupported_currency', 'duplicate_data',
    'out_of_range_day', 'scope_mismatch', 'aggregation_mismatch',
})
SAFE_FAILURE_STAGES = frozenset({
    'unknown', 'preflight', 'http', 'network', 'response', 'schema', 'coverage',
    'pagination', 'scope', 'amount', 'aggregation',
})
COMPLETENESS_CODES = frozenset({
    'incomplete_coverage', 'pagination_invalid', 'pagination_limit', 'response_truncated',
    'no_accounting_data', 'duplicate_data', 'out_of_range_day', 'aggregation_mismatch',
})


class BillingSmokeError(RuntimeError):
    '''A validation or transport restriction that must stop the smoke read.'''


class SafeArgumentParser(argparse.ArgumentParser):
    '''Keep invalid arguments out of terminal error messages.'''

    def error(self, message: str) -> None:
        '''Reject invalid CLI input without echoing an accidentally supplied secret.'''
        raise BillingSmokeError('Invalid arguments')


def schema_metadata(response: requests.Response | None, query_mode: str) -> dict[str, Any]:
    '''Inspect bounded billing structure using only fixed names, types and record counts.'''
    metadata: dict[str, Any] = {
        'query_mode': query_mode if query_mode in {'description_grouped', 'ungrouped'} else 'unknown',
        'json_status': 'no_http_response',
        'limit_reached_type': 'missing', 'limit_reached': None,
        'time_series_type': 'missing', 'time_series_count': None,
        'data_point_count': None, 'usd_value_count': None,
        'invalid_series_count': None, 'invalid_point_count': None,
        'unexpected_top_level_field_count': None, 'metadata_complete': False,
    }
    if response is None:
        return metadata
    if type(response.status_code) is not int or not 200 <= response.status_code < 300:
        metadata['json_status'] = 'non_success_http'
        return metadata
    try:
        body = app.response_json(response, 'xAI')
    except app.ReporterError:
        metadata['json_status'] = 'invalid_json'
        return metadata
    except Exception:
        metadata['json_status'] = 'unavailable'
        return metadata

    def field_type(field: str) -> str:
        '''Classify a known field without returning its contents or an arbitrary key.'''
        if field not in body:
            return 'missing'
        value = body[field]
        if value is None:
            return 'null'
        if type(value) is bool:
            return 'boolean'
        if isinstance(value, (int, Decimal)):
            return 'number'
        if isinstance(value, str):
            return 'string'
        if isinstance(value, list):
            return 'array'
        return 'object'

    metadata.update({
        'json_status': 'valid_object',
        'limit_reached_type': field_type('limitReached'),
        'limit_reached': body['limitReached'] if type(body.get('limitReached')) is bool else None,
        'time_series_type': field_type('timeSeries'),
        'unexpected_top_level_field_count': len(set(body) - {'limitReached', 'timeSeries'}),
    })
    series = body.get('timeSeries')
    if not isinstance(series, list):
        return metadata
    metadata.update({
        'time_series_count': len(series), 'data_point_count': 0, 'usd_value_count': 0,
        'invalid_series_count': 0, 'invalid_point_count': 0, 'metadata_complete': True,
    })
    inspected_points = 0
    for index, group in enumerate(series):
        if index >= app.MAX_BILLING_ROWS:
            metadata['metadata_complete'] = False
            break
        if not isinstance(group, dict) or not isinstance(group.get('dataPoints'), list):
            metadata['invalid_series_count'] += 1
            continue
        points = group['dataPoints']
        metadata['data_point_count'] += len(points)
        for point in points:
            if inspected_points >= app.MAX_BILLING_ROWS:
                metadata['metadata_complete'] = False
                break
            inspected_points += 1
            if (not isinstance(point, dict) or not isinstance(point.get('timestamp'), str)
                    or not isinstance(point.get('values'), list)):
                metadata['invalid_point_count'] += 1
                continue
            metadata['usd_value_count'] += len(point['values'])
        if not metadata['metadata_complete']:
            break
    return metadata


class ReadOnlyBillingSession(requests.Session):
    '''Permit only the selected documented read-only checks with strict HTTPS.'''

    def __init__(self, xai_team_id: str | None, *, diagnostic: bool = False,
                 provider: str = 'both') -> None:
        '''Create a fresh session without ambient proxy, netrc or authentication state.'''
        if (provider not in {'both', 'openai', 'xai'}
                or (not diagnostic and (provider != 'both' or xai_team_id is None))
                or (xai_team_id is not None and (not isinstance(xai_team_id, str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', xai_team_id) is None))):
            raise BillingSmokeError('Invalid billing scope')
        super().__init__()
        self.trust_env = False
        self.request_count = 0
        self.http_statuses: list[int | None] = []
        self.diagnostic = diagnostic
        self.response_schemas: list[dict[str, Any]] = []
        self.transport_error_category: str | None = None
        self.allowed_requests: set[tuple[str, str]] = set()
        if provider in {'both', 'openai'}:
            self.allowed_requests.add(('GET', app.OPENAI_COSTS_URL))
        if provider in {'both', 'xai'}:
            if xai_team_id is not None:
                self.allowed_requests.add(('POST', f'{app.XAI_BASE_URL}/v1/billing/teams/{xai_team_id}/usage'))
            if diagnostic:
                self.allowed_requests.add(('GET', XAI_MANAGEMENT_KEY_VALIDATION_URL))

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        '''Reject writes, other endpoints and redirects before any credential forwarding.'''
        if (not isinstance(method, str) or not isinstance(url, str)
                or (method.upper(), url) not in self.allowed_requests):
            raise BillingSmokeError('A request outside the read-only billing scope was refused')
        kwargs['allow_redirects'] = False
        kwargs['verify'] = True
        if self.request_count >= app.MAX_BILLING_PAGES:
            raise BillingSmokeError('The request safety limit was reached')
        self.request_count += 1
        self.http_statuses.append(None)
        schema_index = None
        query_mode = 'unknown'
        if self.diagnostic and method.upper() == 'POST':
            payload = kwargs.get('json')
            analytics = payload.get('analyticsRequest') if isinstance(payload, dict) else None
            groups = analytics.get('groupBy') if isinstance(analytics, dict) else None
            query_mode = ('description_grouped' if groups == ['description'] else
                          'ungrouped' if groups == [] else 'unknown')
            schema_index = len(self.response_schemas)
            self.response_schemas.append(schema_metadata(None, query_mode))
        try:
            response = super().request(method, url, **kwargs)
        except requests.RequestException as error:
            self.transport_error_category = (
                'timeout' if isinstance(error, requests.Timeout) else
                'tls_error' if isinstance(error, requests.exceptions.SSLError) else
                'connection_error' if isinstance(error, requests.ConnectionError) else 'network_error'
            )
            raise
        if type(response.status_code) is int and 100 <= response.status_code <= 599:
            self.http_statuses[-1] = response.status_code
        if schema_index is not None:
            self.response_schemas[schema_index] = schema_metadata(response, query_mode)
        if 300 <= response.status_code < 400:
            response.close()
            raise BillingSmokeError('Redirected billing responses are not accepted')
        return response


def period_metadata(report_date: date) -> dict[str, dict[str, str]]:
    '''Describe full UTC daily and month-to-date periods with exclusive end times.'''
    month_start, end = app.billing_period_utc(report_date)
    daily_start = end - timedelta(days=1)

    def utc_text(instant: datetime) -> str:
        '''Render an explicitly UTC instant without applying the execution timezone.'''
        return instant.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    return {
        'daily': {'start_utc': utc_text(daily_start), 'end_utc_exclusive': utc_text(end)},
        'month_to_date': {'start_utc': utc_text(month_start), 'end_utc_exclusive': utc_text(end)},
    }


def offline_plan(report_date: date) -> dict[str, Any]:
    '''Describe the future read without inspecting credentials or other environment values.'''
    app.validate_billing_date(report_date)
    return {
        'status': 'prepared_offline',
        'network_requests': False,
        'report_date_utc': report_date.isoformat(),
        'periods': period_metadata(report_date),
        'requests': [
            {'provider': 'OpenAI', 'method': 'GET', 'endpoint': app.OPENAI_COSTS_URL},
            {'provider': 'xAI', 'method': 'POST',
             'endpoint': f'{app.XAI_BASE_URL}/v1/billing/teams/<XAI_TEAM_ID>/usage',
             'purpose': 'Read-only billing analytics query',
             'conditional_zero_confirmation':
                 'An explicit empty grouped result triggers the same daily USD query with groupBy=[]; '
                 'complete explicit zero records confirm zero; two valid empty datasets mean '
                 'NO_RECORDED_USAGE and $0 recorded spending, subject to delayed billing and rechecking'},
        ],
        'required_environment': ['OPENAI_ADMIN_KEY', 'XAI_MANAGEMENT_KEY', 'XAI_TEAM_ID'],
        'optional_environment': {'OPENAI_PROJECT_IDS': 'Blank selects the organization; otherwise selected projects'},
        'live_read': 'Use --live only after authorization and replacing previously disclosed keys',
        'files_written': False,
        'delivery_performed': False,
    }


def read_billing_costs(report_date: date) -> dict[str, Any]:
    '''Fetch both cost APIs only after all environment and date inputs pass preflight.'''
    configuration = app.validate_billing_configuration(report_date)
    with ReadOnlyBillingSession(configuration.xai_team_id) as session:
        report = app.get_live_report(session, report_date)
    if (report.demo or report.report_date != report_date
            or report.openai_project_ids != configuration.openai_project_ids):
        raise BillingSmokeError('The returned report does not match the validated scope')
    result = {
        'status': 'provider_costs_confirmed',
        'network_requests': True,
        'report_date_utc': report_date.isoformat(),
        'generated_at_utc': report.generated_at.isoformat(),
        'periods': period_metadata(report_date),
        'currency': 'USD',
        'data_type': 'PROVIDER_REPORTED_API_SPEND',
        'openai_scope': {
            'type': 'selected_projects' if configuration.openai_project_ids else 'all_organization_projects',
            'project_ids': list(configuration.openai_project_ids),
        },
        'xai_scope': {'type': 'configured_team', 'team_id': configuration.xai_team_id},
        'providers': {
            'openai': {'daily_usd': app.usd_text(report.openai.daily),
                       'month_to_date_usd': app.usd_text(report.openai.month_to_date)},
            'xai': {'daily_usd': app.usd_text(report.xai.daily),
                    'month_to_date_usd': app.usd_text(report.xai.month_to_date)},
        },
        'daily_total_usd': app.usd_text(app.exact_sum((report.openai.daily, report.xai.daily))),
        'month_to_date_total_usd': app.usd_text(app.exact_sum((report.openai.month_to_date, report.xai.month_to_date))),
        'billing_finality': 'Provider records may arrive late or be adjusted; this read does not establish invoice finality',
        'files_written': False,
        'delivery_performed': False,
    }
    evidence = billing_evidence(report.xai, 'xai')
    if evidence != 'explicit_daily_cost_records':
        result['providers']['xai']['accounting_evidence'] = evidence
    if report.xai.accounting_state == app.NO_RECORDED_USAGE:
        result.update({
            'status': 'recorded_spending_snapshot', 'reconciliation_status': 'not_reconciled',
            'recorded_usage_note': app.XAI_NO_RECORDED_USAGE_NOTE,
        })
        result['providers']['xai'].update({
            'accounting_state': app.NO_RECORDED_USAGE,
            'reconciliation_status': 'not_reconciled', 'allow_recheck': True,
        })
    return result


def diagnostic_evidence(session: ReadOnlyBillingSession | None) -> dict[str, Any]:
    '''Expose dispatch evidence and fixed schema counts without request or response contents.'''
    statuses = list(session.http_statuses) if session is not None else []
    return {
        'request_attempted': bool(session is not None and session.request_count),
        'requests_attempted': session.request_count if session is not None else 0,
        'http_status': statuses[-1] if statuses else None,
        'http_statuses': statuses,
        'response_schemas': list(session.response_schemas) if session is not None else [],
    }


def diagnostic_failure(error: Exception, session: ReadOnlyBillingSession | None) -> dict[str, Any]:
    '''Classify trusted codes and transport evidence without serializing an exception.'''
    evidence = diagnostic_evidence(session)
    code = error.code if isinstance(error, app.ReporterError) and error.code in SAFE_ERROR_CODES else 'internal_error'
    stage = (error.failure_stage if isinstance(error, app.ReporterError)
             and error.failure_stage in SAFE_FAILURE_STAGES else 'unknown')
    status = evidence['http_status']
    category = code
    if session is not None and session.transport_error_category:
        category, stage = session.transport_error_category, 'network'
    elif status is not None and not 200 <= status < 300:
        stage = 'http'
        category = (
            'authentication_rejected' if status == 401 else
            'permission_rejected' if status == 403 else
            'invalid_request' if status in {400, 422} else
            'endpoint_or_scope_not_found' if status == 404 else
            'rate_limited' if status == 429 else
            'provider_error' if status >= 500 else
            'redirect_rejected' if 300 <= status < 400 else 'http_error'
        )
    details = error.details if isinstance(error, app.ReporterError) else {}
    missing = details.get('missing_utc_days', [])
    # Revalidate every diagnostic output rather than trusting exception attributes.
    safe_missing = []
    if isinstance(missing, list) and len(missing) <= 31:
        for value in missing:
            if isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                try:
                    if date.fromisoformat(value).isoformat() == value:
                        safe_missing.append(value)
                except ValueError:
                    continue
    return {
        **evidence, 'status': 'failed', 'failure_stage': stage, 'failure_code': code,
        'error_category': category,
        'authentication_rejected': category == 'authentication_rejected',
        'permission_rejected': category == 'permission_rejected',
        'invalid_request_rejected': category == 'invalid_request',
        'invalid_response_schema': code in {'invalid_json', 'response_schema_invalid'},
        'response_completeness_failed': code in COMPLETENESS_CODES,
        'pagination_failed': code in {'pagination_invalid', 'pagination_limit'},
        'missing_utc_days': sorted(set(safe_missing)),
        'accounting_state': 'UNKNOWN',
        'daily_usage_state': 'unknown', 'month_to_date_usage_state': 'unknown',
    }


def billing_evidence(spend: app.ProviderSpend, provider: str) -> str:
    '''Validate accounting state and whitelist evidence without exposing arbitrary metadata.'''
    app.validate_accounting_evidence(spend, provider)
    evidence = getattr(spend, 'accounting_evidence', None)
    if evidence is None:
        return 'explicit_daily_cost_records'
    if (isinstance(evidence, str) and evidence == app.XAI_ZERO_CONFIRMATION_EVIDENCE
            and spend.daily.is_zero() and spend.month_to_date.is_zero() and not spend.daily_breakdown):
        return evidence
    if evidence == app.XAI_EMPTY_USAGE_EVIDENCE and spend.accounting_state == app.NO_RECORDED_USAGE:
        return evidence
    raise app.ReporterError('Cost accounting evidence is invalid', code='response_schema_invalid', stage='schema')


def diagnostic_cost_success(spend: app.ProviderSpend, session: ReadOnlyBillingSession,
                            *, provider: str) -> dict[str, Any]:
    '''Emit exact amounts only after the unchanged production accounting checks pass.'''
    evidence = billing_evidence(spend, provider)
    zero_confirmed = evidence == app.XAI_ZERO_CONFIRMATION_EVIDENCE
    inactive = spend.accounting_state == app.NO_RECORDED_USAGE
    result = {
        **diagnostic_evidence(session), 'status': 'recorded_usage_absent' if inactive else 'costs_confirmed',
        'failure_stage': None, 'failure_code': None, 'error_category': None,
        'authentication_rejected': False, 'permission_rejected': False,
        'invalid_request_rejected': False, 'invalid_response_schema': False,
        'response_completeness_failed': False, 'pagination_failed': False,
        'missing_utc_days': [], 'coverage': 'valid_empty_datasets' if inactive else 'complete', 'currency': 'USD',
        'daily_usage_state': ('no_recorded_usage' if inactive else
                              'confirmed_zero' if spend.daily.is_zero() else 'confirmed_recorded_cost'),
        'month_to_date_usage_state': ('no_recorded_usage' if inactive else
                                      'confirmed_zero' if spend.month_to_date.is_zero() else 'confirmed_recorded_cost'),
        'daily_usd': app.usd_text(spend.daily), 'month_to_date_usd': app.usd_text(spend.month_to_date),
        'accounting_evidence': evidence,
        'accounting_state': spend.accounting_state,
        'zero_confirmation_attempted': zero_confirmed or inactive,
        'zero_confirmation_confirmed': zero_confirmed,
    }
    if inactive:
        result.update({'reconciliation_status': 'not_reconciled', 'allow_recheck': True,
                       'recorded_usage_note': app.XAI_NO_RECORDED_USAGE_NOTE})
    return result


def validate_management_key(session: ReadOnlyBillingSession, management_key: str,
                            team_id: str | None = None) -> dict[str, str]:
    '''Validate Management-key acceptance and return only scope/linkage enums.'''
    response = session.get(XAI_MANAGEMENT_KEY_VALIDATION_URL,
                           headers={'Authorization': f'Bearer {management_key}'},
                           timeout=30, allow_redirects=False, verify=True)
    if 200 <= response.status_code < 300 and (not isinstance(response.content, bytes)
            or not response.content or len(response.content) > MAX_KEY_VALIDATION_BYTES):
        raise app.ReporterError('Invalid Management-key response size',
                                code='response_schema_invalid', stage='response')
    body = app.response_json(response, 'xAI')
    if (not isinstance(body.get('apiKeyId'), str) or not body['apiKeyId']
            or len(body['apiKeyId']) > 256
            or any(ord(character) <= 32 or ord(character) >= 127 for character in body['apiKeyId'])):
        raise app.ReporterError('Invalid Management-key metadata', code='response_schema_invalid', stage='schema')
    scopes = {'SCOPE_TEAM': 'team', 'SCOPE_ORGANIZATION': 'organization', 'SCOPE_UNSPECIFIED': 'unspecified'}
    scope = body.get('scope')
    if 'scope' in body and (not isinstance(scope, str) or scope not in scopes):
        raise app.ReporterError('Invalid Management-key scope', code='response_schema_invalid', stage='schema')
    for field in ('scopeId', 'teamId'):
        if field in body and (not isinstance(body[field], str) or len(body[field]) > 256):
            raise app.ReporterError('Invalid Management-key scope metadata', code='response_schema_invalid', stage='schema')
    if 'acls' in body and (not isinstance(body['acls'], list) or len(body['acls']) > 1024
            or not all(isinstance(value, str) and len(value) <= 256 for value in body['acls'])):
        raise app.ReporterError('Invalid Management-key permission metadata', code='response_schema_invalid', stage='schema')
    linkage = 'not_configured' if team_id is None else 'unknown'
    if team_id is not None and scope == 'SCOPE_TEAM' and body.get('scopeId'):
        linkage = 'match' if body['scopeId'] == team_id else 'mismatch'
        if body.get('teamId') and body['teamId'] != body['scopeId']:
            linkage = 'mismatch'
    return {'key_scope': scopes.get(scope, 'unknown'), 'team_linkage': linkage}


def diagnose_openai(report_date: date) -> dict[str, dict[str, Any]]:
    '''Read OpenAI costs independently while retaining the configured project scope.'''
    session = None
    try:
        configuration = app.validate_openai_billing_configuration(report_date)
        with ReadOnlyBillingSession(None, diagnostic=True, provider='openai') as session:
            spend = app.fetch_openai_costs(session, configuration.admin_key,
                                          report_date, configuration.project_ids)
            result = diagnostic_cost_success(spend, session, provider='openai')
        result['openai_scope'] = 'selected_projects' if configuration.project_ids else 'organization'
        result['configured_project_count'] = len(configuration.project_ids)
    except Exception as error:
        result = diagnostic_failure(error, session)
    return {'openai_costs': result}


def diagnose_xai(report_date: date) -> dict[str, dict[str, Any]]:
    '''Test Management-key acceptance and historical permission as separate reads.'''
    session = None
    try:
        management_key = app.billing_header_key('XAI_MANAGEMENT_KEY')
        raw_team = os.environ.get('XAI_TEAM_ID', '').strip()
        team_id = raw_team if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', raw_team, re.ASCII) else None
        with ReadOnlyBillingSession(None, diagnostic=True, provider='xai') as session:
            metadata = validate_management_key(session, management_key, team_id)
            key_result = {
                **diagnostic_evidence(session), 'status': 'key_confirmed', 'key_accepted': True,
                'failure_stage': None, 'failure_code': None, 'error_category': None,
                'authentication_rejected': False, 'permission_rejected': False,
                'invalid_request_rejected': False, 'invalid_response_schema': False,
                'response_completeness_failed': False, 'pagination_failed': False,
                'missing_utc_days': [], 'daily_usage_state': 'unknown',
                'month_to_date_usage_state': 'unknown', **metadata,
            }
            if metadata['team_linkage'] == 'mismatch':
                key_result.update(diagnostic_failure(app.ReporterError(
                    'Management-key scope differs', code='scope_mismatch', stage='scope'), session))
    except Exception as error:
        key_result = diagnostic_failure(error, session)
    # A metadata rejection is not a historical billing test. Always attempt the
    # billing check independently when its own local configuration is valid.
    session = None
    try:
        configuration = app.validate_xai_billing_configuration(report_date)
        with ReadOnlyBillingSession(configuration.team_id, diagnostic=True, provider='xai') as session:
            spend = app.fetch_xai_costs(session, configuration.management_key,
                                       configuration.team_id, report_date)
            billing_result = diagnostic_cost_success(spend, session, provider='xai')
    except Exception as error:
        billing_result = diagnostic_failure(error, session)
        billing_result.update({
            'zero_confirmation_attempted': bool(session is not None and session.request_count >= 2),
            'zero_confirmation_confirmed': False,
        })
    return {'xai_management_key_validation': key_result, 'xai_billing': billing_result}


def diagnose_billing(report_date: date, provider: str = 'both') -> dict[str, Any]:
    '''Collect independent read-only checks without creating a combined cost report.'''
    app.validate_billing_date(report_date)
    if provider not in {'both', 'openai', 'xai'}:
        raise BillingSmokeError('Invalid provider selection')
    checks = {}
    if provider in {'both', 'openai'}:
        checks.update(diagnose_openai(report_date))
    if provider in {'both', 'xai'}:
        checks.update(diagnose_xai(report_date))
    accepted = all(check['status'] in {'costs_confirmed', 'key_confirmed', 'recorded_usage_absent'}
                   for check in checks.values())
    inactive = any(check['status'] == 'recorded_usage_absent' for check in checks.values())
    return {
        'status': ('diagnostics_recorded_usage_absent' if accepted and inactive else
                   'diagnostics_confirmed' if accepted else 'diagnostics_failed'),
        'diagnostic': True, 'provider_selection': provider, 'report_date_utc': report_date.isoformat(),
        'periods': period_metadata(report_date), 'checks': checks,
        'network_requests': any(check['request_attempted'] for check in checks.values()),
        'attempt_semantics': 'Transport dispatch attempted; this does not prove the provider received it',
        'files_written': False, 'delivery_performed': False,
    }


def main(argv: list[str] | None = None) -> int:
    '''Print an offline plan by default, or a separately authorized read with --live.'''
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true',
                        help='Perform the selected read-only checks using protected process environment values')
    parser.add_argument('--date', help='Completed UTC day, YYYY-MM-DD; default yesterday UTC')
    parser.add_argument('--diagnostic', action='store_true',
                        help='Report safe, independent provider checks instead of a combined cost read')
    parser.add_argument('--provider', choices=('both', 'openai', 'xai'),
                        help='Select diagnostic checks; defaults to both, requires --diagnostic')
    args = None
    try:
        args = parser.parse_args(argv)
        if args.provider is not None and not args.diagnostic:
            raise BillingSmokeError('Provider selection requires diagnostics')
        try:
            report_date = (date.fromisoformat(args.date) if args.date else
                           datetime.now(timezone.utc).date() - timedelta(days=1))
        except ValueError:
            raise app.ReporterError('Invalid report date', code='invalid_report_date', stage='preflight') from None
        app.validate_billing_date(report_date)
        if args.diagnostic and args.live:
            result = diagnose_billing(report_date, args.provider or 'both')
        else:
            result = read_billing_costs(report_date) if args.live else offline_plan(report_date)
            if args.diagnostic:
                result.update({'diagnostic': True, 'provider_selection': args.provider or 'both'})
                selected = args.provider or 'both'
                result['required_environment'] = (
                    ['OPENAI_ADMIN_KEY'] if selected == 'openai' else
                    ['XAI_MANAGEMENT_KEY', 'XAI_TEAM_ID'] if selected == 'xai' else result['required_environment']
                )
                if selected == 'xai':
                    result['optional_environment'] = {}
                result['requests'] = [request for request in result['requests']
                                      if selected == 'both' or request['provider'].lower() == selected]
                if selected in {'both', 'xai'}:
                    result['requests'].insert(-1, {'provider': 'xAI', 'method': 'GET',
                        'endpoint': XAI_MANAGEMENT_KEY_VALIDATION_URL, 'purpose': 'Read-only Management-key validation'})
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 1 if result['status'] == 'diagnostics_failed' else 0
    except Exception as error:
        if args is not None and args.diagnostic:
            failure = diagnostic_failure(error, None)
            preflight_only = not args.live or failure['failure_stage'] == 'preflight'
            if not preflight_only:
                # Unexpected top-level failures have no retained transport
                # evidence. Do not assert that no request could have occurred.
                failure.update({'request_attempted': None, 'requests_attempted': None})
            print(json.dumps({
                'status': 'diagnostics_failed', 'diagnostic': True,
                'network_requests': False if preflight_only else None, 'checks': {},
                'global_error': failure,
                'files_written': False, 'delivery_performed': False,
            }, indent=2, sort_keys=True, allow_nan=False))
            return 1
        # Provider/transport/config exceptions can contain secrets. Deliberately
        # report only a fixed failure code; no partial totals or traceback.
        print('BILLING_SMOKE_FAILED: no complete cost read was confirmed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
