'''Offline regressions for exact billing amounts, explicit scope and complete UTC coverage.'''

from __future__ import annotations

import copy
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_DOWN, Rounded, localcontext
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlsplit

import requests

import app


REPORT_DATE = date(2026, 10, 6)
ENVIRONMENT = {
    'OPENAI_ADMIN_KEY': 'offline-openai-placeholder',
    'XAI_MANAGEMENT_KEY': 'offline-xai-placeholder',
    'XAI_TEAM_ID': 'team_offline',
    'OPENAI_PROJECT_IDS': '',
}


class RawResponse:
    '''A raw-byte response that forbids the float-producing Response.json path.'''

    def __init__(self, body: dict[str, Any] | bytes, status: int = 200) -> None:
        '''Store mocked provider data only, never contacting an endpoint.'''
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.status_code = status

    def json(self) -> None:
        '''Fail if code bypasses raw JSON Decimal decoding.'''
        raise AssertionError('Billing must decode raw response bytes')


class Session:
    '''Capture request safety settings and return queued offline responses.'''

    def __init__(self, gets: list[RawResponse] | None = None,
                 posts: list[RawResponse] | None = None) -> None:
        '''Retain deterministic queues and independent request snapshots.'''
        self.gets = list(gets or [])
        self.posts = list(posts or [])
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.trust_env = True

    def get(self, url: str, **kwargs: Any) -> RawResponse:
        '''Return one mocked GET response.'''
        self.calls.append(('GET', url, copy.deepcopy(kwargs)))
        return self.gets.pop(0)

    def post(self, url: str, **kwargs: Any) -> RawResponse:
        '''Return one mocked analytics POST response.'''
        self.calls.append(('POST', url, copy.deepcopy(kwargs)))
        return self.posts.pop(0)


def record(value: object, label: str = 'Tokens', project: str | None = None) -> dict[str, Any]:
    '''Create one official-shaped grouped USD cost result.'''
    return {'object': 'organization.costs.result', 'amount': {'value': value, 'currency': 'usd'},
            'line_item': label, 'project_id': project}


def bucket(day: int, results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    '''Create a complete UTC bucket; an explicit empty result is a covered zero day.'''
    timestamp = int(datetime(2026, 10, day, tzinfo=timezone.utc).timestamp())
    return {'object': 'bucket', 'start_time': timestamp, 'end_time': timestamp + 86400,
            'results': results or []}


def openai_page(buckets: list[dict[str, Any]] | None = None, more: bool = False,
                cursor: str | None = None) -> dict[str, Any]:
    '''Create documented costs-page metadata and six-day coverage by default.'''
    return {'object': 'page', 'data': buckets if buckets is not None else [bucket(day) for day in range(1, 7)],
            'has_more': more, 'next_page': cursor}


def series(label: str = 'Grok', values: dict[int, object] | None = None) -> dict[str, Any]:
    '''Create the documented dense daily USD analytics series.'''
    return {'groupLabels': [label], 'dataPoints': [
        {'timestamp': f'2026-10-{day:02d}T00:00:00Z', 'values': [(values or {}).get(day, '0')]}
        for day in range(1, 7)
    ]}


def xai_body(groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    '''Create explicit complete analytics with a dense covered zero group by default.'''
    return {'limitReached': False, 'timeSeries': groups if groups is not None else [series()]}


class ExactMoneyTests(unittest.TestCase):
    '''Protect raw precision, exact totals and deterministic display and snapshots.'''

    def test_raw_json_numeric_token_never_becomes_float(self) -> None:
        '''The full original numeric token survives beyond Decimal's default 28 digits.'''
        digits = '0.123456789012345678901234567890123456789'
        result = app.response_json(RawResponse(('{"amount":' + digits + ',"count":3}').encode()), 'OpenAI')
        self.assertIsInstance(result['amount'], Decimal)
        self.assertEqual(result['amount'], Decimal(digits))
        self.assertIs(type(result['count']), int)

    def test_ambiguous_or_invalid_json_is_rejected(self) -> None:
        '''Duplicate keys and invalid JSON extensions cannot alter accounting silently.'''
        for content in (b'{"value":1,"value":2}', b'{"value":NaN}', b'{"value":Infinity}',
                        b'{"value":-Infinity}', b'[]', b'null', b'{"value":"\xff"}', b'{'):
            with self.subTest(content=content), self.assertRaises(app.ReporterError):
                app.response_json(RawResponse(content), 'OpenAI')

    def test_response_status_and_size_fail_closed_without_body_disclosure(self) -> None:
        '''Redirects, failures, empty and oversized responses are not spend records.'''
        for status in (301, 302, 307, 400, 401, 403, 429, 500):
            with self.subTest(status=status), self.assertRaises(app.ReporterError) as raised:
                app.response_json(RawResponse(b'{"private":"offline-body-marker"}', status), 'OpenAI')
            self.assertNotIn('offline-body-marker', str(raised.exception))
        for content in (b'', b' ' * 33):
            with patch.object(app, 'MAX_BILLING_RESPONSE_BYTES', 32), self.assertRaises(app.ReporterError):
                app.response_json(RawResponse(content), 'OpenAI')

    def test_exact_sum_and_report_totals_ignore_low_precision_and_traps(self) -> None:
        '''Even rounding traps in callers cannot truncate real provider totals.'''
        a = Decimal('123456789012345678901234567890.123456789')
        b = Decimal('0.000000001')
        report = app.BillingReport(REPORT_DATE, app.ProviderSpend(a, a), app.ProviderSpend(b, b),
                                   datetime(2026, 10, 7, tzinfo=timezone.utc))
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            self.assertEqual(app.exact_sum((a, b)), Decimal('123456789012345678901234567890.123456790'))
            self.assertEqual(report.daily_total, Decimal('123456789012345678901234567890.123456790'))
            self.assertEqual(report.month_to_date_total, report.daily_total)
            self.assertEqual(app.money(Decimal('1.235')), '$1.24')
            self.assertEqual(app.money(Decimal('1.245')), '$1.24')

    def test_negative_provider_adjustments_are_preserved(self) -> None:
        '''Signed recorded refunds remain signed and exact.'''
        self.assertEqual(app.exact_sum((Decimal('5.01'), Decimal('-1.000000001'))), Decimal('4.009999999'))
        self.assertEqual(app.money(Decimal('-1.235')), '$-1.24')
        self.assertEqual(app.exact_sum(()), Decimal('0'))

    def test_malformed_or_unbounded_money_is_rejected(self) -> None:
        '''No float rounding, non-finite values, pathological exponents or loose strings.'''
        malformed = (None, True, False, 0.1, 'NaN', 'Infinity', '-Infinity', '1e999999999',
                     '1e-999999999', '1_000', '\u0661.\u0662', ' 1', '1 ', '+1', '01', '.1', '1.',
                     '9' * 1025, Decimal('NaN'), Decimal('Infinity'))
        for value in malformed:
            with self.subTest(value=repr(value)[:80]), self.assertRaises(app.ReporterError):
                app.decimal_value(value, 'Offline')
        self.assertEqual(app.decimal_value('1e-20', 'Offline'), Decimal('1e-20'))

    def test_breakdown_order_and_other_sum_are_context_independent(self) -> None:
        '''Close high-precision costs stay correctly ranked, including the omitted remainder.'''
        values = {'A smaller': Decimal('1.000000000000000000001'),
                  'Z larger': Decimal('1.000000000000000000002'),
                  'Refund': Decimal('-0.005'), 'Other cost': Decimal('0.010')}
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            rows = app.breakdown_rows(values, max_rows=1)
        self.assertEqual(rows[0][0], 'Z larger')
        self.assertEqual(rows[1], ['Other (3 line items)', '$1.01'])

    def test_snapshot_usd_text_is_lossless_fixed_point_under_capitals_changes(self) -> None:
        '''Exact snapshot monetary bytes do not depend on Decimal exponent capitalization.'''
        small = Decimal('1e-20')
        report = app.BillingReport(REPORT_DATE, app.ProviderSpend(small, small, {'Tiny': small}),
                                   app.ProviderSpend(small, small), datetime(2026, 10, 7, tzinfo=timezone.utc))
        snapshots = []
        for capitals in (0, 1):
            with localcontext() as context:
                context.capitals = capitals
                context.prec = 2
                snapshots.append(json.dumps(app.report_to_dict(report), sort_keys=True))
        self.assertEqual(snapshots[0], snapshots[1])
        data = json.loads(snapshots[0])
        self.assertEqual(data['providers']['openai']['daily_usd'], '0.00000000000000000001')
        self.assertEqual(data['providers']['openai']['daily_breakdown_usd']['Tiny'], '0.00000000000000000001')
        self.assertEqual(data['daily_total_usd'], '0.00000000000000000002')
        self.assertEqual(app.report_from_dict(data, REPORT_DATE).daily_total, Decimal('2e-20'))


class OpenAIScopeAndCoverageTests(unittest.TestCase):
    '''Require official query encoding, explicit project proof and full costs-page coverage.'''

    def test_selected_projects_use_current_sdk_bracket_wire_encoding(self) -> None:
        '''Both array fields use repeated bracketed names, with project grouping.'''
        data = openai_page()
        data['data'][5]['results'] = [record('1.1', project='proj_offline_a'),
                                      record('2.2', project='proj_offline_b')]
        session = Session(gets=[RawResponse(data)])
        result = app.fetch_openai_costs(session, 'offline-key', REPORT_DATE,
                                       ('proj_offline_a', 'proj_offline_b'))
        params = session.calls[0][2]['params']
        prepared = requests.Request('GET', app.OPENAI_COSTS_URL, params=params).prepare()
        query = urlsplit(prepared.url).query
        self.assertIn('project_ids%5B%5D=proj_offline_a&project_ids%5B%5D=proj_offline_b', query)
        self.assertIn('group_by%5B%5D=line_item&group_by%5B%5D=project_id', query)
        pairs = parse_qsl(query)
        self.assertNotIn('project_ids', [name for name, _ in pairs])
        self.assertNotIn('group_by', [name for name, _ in pairs])
        self.assertEqual(result.daily, Decimal('3.3'))
        self.assertEqual(result.daily_breakdown['Tokens'], Decimal('3.3'))

    def test_organization_scope_omits_filter_and_project_grouping(self) -> None:
        '''Blank configuration remains explicitly organization-wide.'''
        session = Session(gets=[RawResponse(openai_page())])
        app.fetch_openai_costs(session, 'offline-key', REPORT_DATE)
        params = session.calls[0][2]['params']
        self.assertNotIn('project_ids[]', params)
        self.assertEqual(params['group_by[]'], ['line_item'])

    def test_scoped_records_cannot_be_global_missing_or_unrequested(self) -> None:
        '''A requested scope is verified on every cost record before totals are published.'''
        for project in (None, 'proj_other', '', 3):
            data = openai_page()
            data['data'][5]['results'] = [record('1', project=project)]
            with self.subTest(project=project), self.assertRaisesRegex(app.ReporterError, 'scope'):
                app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE,
                                       ('proj_offline_a',))
        data['data'][5]['results'][0].pop('project_id')
        with self.assertRaisesRegex(app.ReporterError, 'scope'):
            app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE,
                                   ('proj_offline_a',))

    def test_raw_numeric_costs_and_refund_aggregate_exactly(self) -> None:
        '''Real JSON number tokens are preserved through daily, MTD and grouped aggregation.'''
        data = openai_page()
        data['data'][4]['results'] = [record('5', 'Prior')]
        data['data'][5]['results'] = [record('RAW', 'Tokens'), record('-0.000000001', 'Refund')]
        raw = json.dumps(data).replace('"RAW"', '1.123456789012345678901234567890123456789').encode()
        result = app.fetch_openai_costs(Session(gets=[RawResponse(raw)]), 'offline-key', REPORT_DATE)
        self.assertEqual(result.daily, Decimal('1.123456788012345678901234567890123456789'))
        self.assertEqual(result.month_to_date, Decimal('6.123456788012345678901234567890123456789'))
        self.assertEqual(result.daily_breakdown['Tokens'], Decimal('1.123456789012345678901234567890123456789'))

    def test_explicit_zero_buckets_pass_but_missing_or_empty_coverage_fails(self) -> None:
        '''Missing billing days never silently become zero, including missing first/last days.'''
        self.assertEqual(app.fetch_openai_costs(Session(gets=[RawResponse(openai_page())]),
                                               'offline-key', REPORT_DATE).daily, Decimal('0'))
        for buckets in ([], [bucket(day) for day in range(2, 7)],
                        [bucket(day) for day in range(1, 6)], [bucket(6)]):
            with self.subTest(days=len(buckets)), self.assertRaisesRegex(app.ReporterError, 'coverage'):
                app.fetch_openai_costs(Session(gets=[RawResponse(openai_page(buckets))]),
                                       'offline-key', REPORT_DATE)

    def test_bucket_boundaries_must_be_unique_complete_requested_utc_days(self) -> None:
        '''Off-by-one seconds, shifted days, bool/string timestamps and the end boundary fail.'''
        for field, value in (('start_time', True), ('start_time', '1791244800'),
                             ('start_time', bucket(6)['start_time'] + 1),
                             ('start_time', bucket(7)['start_time']),
                             ('end_time', bucket(6)['end_time'] - 1),
                             ('end_time', bucket(6)['end_time'] + 1), ('end_time', None)):
            data = openai_page()
            data['data'][5][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(app.ReporterError, 'UTC day'):
                app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE)

    def test_duplicate_bucket_across_pages_and_duplicate_group_records_fail(self) -> None:
        '''Retries or overlapping pagination cannot double-charge grouped amounts.'''
        responses = [RawResponse(openai_page([bucket(day) for day in range(1, 6)], True, 'next')),
                     RawResponse(openai_page([bucket(5), bucket(6)]))]
        with self.assertRaisesRegex(app.ReporterError, 'repeated a UTC bucket'):
            app.fetch_openai_costs(Session(gets=responses), 'offline-key', REPORT_DATE)
        data = openai_page()
        data['data'][5]['results'] = [record('1'), record('2')]
        with self.assertRaisesRegex(app.ReporterError, 'grouped cost record'):
            app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE)

    def test_final_cursor_is_optional_but_has_more_and_consistency_are_required(self) -> None:
        '''Match the current SDK optional cursor while rejecting ambiguous completion.'''
        data = openai_page()
        data.pop('next_page')
        self.assertEqual(app.fetch_openai_costs(Session(gets=[RawResponse(data)]),
                                               'offline-key', REPORT_DATE).month_to_date, Decimal('0'))
        for change in ({'has_more': None}, {'has_more': 0}, {'has_more': False, 'next_page': 'extra'},
                       {'has_more': True, 'next_page': None}, {'has_more': True, 'next_page': ''}):
            data = openai_page()
            data.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(app.ReporterError, 'pagination'):
                app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE)
        data = openai_page()
        data.pop('has_more')
        with self.assertRaisesRegex(app.ReporterError, 'pagination'):
            app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE)

    def test_cursor_loop_and_empty_continuation_fail(self) -> None:
        '''A cursor must make progress and cannot repeat indefinitely.'''
        responses = [RawResponse(openai_page([bucket(1)], True, 'same')),
                     RawResponse(openai_page([bucket(2)], True, 'same'))]
        with self.assertRaisesRegex(app.ReporterError, 'cursor'):
            app.fetch_openai_costs(Session(gets=responses), 'offline-key', REPORT_DATE)
        with self.assertRaisesRegex(app.ReporterError, 'cursor'):
            app.fetch_openai_costs(Session(gets=[RawResponse(openai_page([], True, 'next'))]),
                                   'offline-key', REPORT_DATE)

    def test_invalid_cost_record_currency_and_missing_amount_fail(self) -> None:
        '''Malformed records cannot be skipped or treated as a zero amount.'''
        for replacement in ({'object': 'wrong'}, {'object': 'organization.costs.result'},
                            record(None), record(True), record('NaN')):
            data = openai_page()
            data['data'][5]['results'] = [replacement]
            with self.subTest(replacement=replacement), self.assertRaises(app.ReporterError):
                app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE)
        for currency in (None, 'EUR', 1):
            item = record('1')
            item['amount']['currency'] = currency
            data = openai_page()
            data['data'][5]['results'] = [item]
            with self.subTest(currency=currency), self.assertRaisesRegex(app.ReporterError, 'non-USD'):
                app.fetch_openai_costs(Session(gets=[RawResponse(data)]), 'offline-key', REPORT_DATE)


class XAIDenseCoverageTests(unittest.TestCase):
    '''Respect the documented exclusive endpoint and dense per-series day coverage.'''

    def test_exclusive_next_midnight_payload_and_dense_raw_decimal_values(self) -> None:
        '''The read-only analytics query includes the full last UTC day without precision loss.'''
        body = xai_body([series(values={5: '2', 6: 'RAW'})])
        raw = json.dumps(body).replace('"RAW"', '0.123456789012345678901234567890123456789').encode()
        session = Session(posts=[RawResponse(raw)])
        spend = app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(url, app.XAI_BASE_URL + '/v1/billing/teams/team_offline/usage')
        analytics = kwargs['json']['analyticsRequest']
        self.assertEqual(analytics['timeRange'], {'startTime': '2026-10-01 00:00:00',
                                                 'endTime': '2026-10-07 00:00:00', 'timezone': 'Etc/GMT'})
        self.assertEqual(analytics['timeUnit'], 'TIME_UNIT_DAY')
        self.assertEqual(analytics['values'], [{'name': 'usd', 'aggregation': 'AGGREGATION_SUM'}])
        self.assertEqual(spend.daily, Decimal('0.123456789012345678901234567890123456789'))
        self.assertEqual(spend.month_to_date, Decimal('2.123456789012345678901234567890123456789'))

    def test_explicit_dense_zero_and_no_recorded_usage_are_distinct(self) -> None:
        '''Covered zero points and two absent record collections retain different evidence.'''
        spend = app.fetch_xai_costs(Session(posts=[RawResponse(xai_body())]),
                                    'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(spend.daily, Decimal('0'))
        self.assertEqual(spend.month_to_date, Decimal('0'))
        self.assertEqual(spend.accounting_state, app.RECORDED_SPENDING)
        inactive = app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([])), RawResponse(xai_body([]))]),
                                       'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(inactive.accounting_state, app.NO_RECORDED_USAGE)
        self.assertEqual(inactive.accounting_evidence, app.XAI_EMPTY_USAGE_EVIDENCE)
        self.assertEqual(inactive.daily_breakdown, {})
        with self.assertRaises(app.ReporterError):
            app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([
                {'groupLabels': ['Grok'], 'dataPoints': []},
            ]))]), 'offline-key', 'team_offline', REPORT_DATE)

    def test_every_group_must_be_dense_even_if_union_covers_all_days(self) -> None:
        '''Two sparse groups cannot conceal incomplete accounting behind their union.'''
        a, b = series('A'), series('B')
        a['dataPoints'] = a['dataPoints'][:3]
        b['dataPoints'] = b['dataPoints'][3:]
        with self.assertRaisesRegex(app.ReporterError, 'coverage'):
            app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([a, b]))]),
                                'offline-key', 'team_offline', REPORT_DATE)
        a, b = series('A'), series('B')
        b['dataPoints'].pop(2)
        with self.assertRaisesRegex(app.ReporterError, 'coverage'):
            app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([a, b]))]),
                                'offline-key', 'team_offline', REPORT_DATE)

    def test_next_boundary_prior_day_fractional_and_partial_days_are_rejected(self) -> None:
        '''Nothing outside the requested half-open daily interval is silently ignored.'''
        invalid = ('2026-10-07T00:00:00Z', '2026-09-30T00:00:00Z',
                   '2026-10-06T00:00:00.1Z', '2026-10-06T23:59:59Z',
                   '2026-10-06T00:00:00', 'not-a-date')
        for timestamp in invalid:
            group = series()
            group['dataPoints'][5]['timestamp'] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(app.ReporterError):
                app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([group]))]),
                                    'offline-key', 'team_offline', REPORT_DATE)

    def test_aware_offsets_are_normalized_to_exact_utc_day(self) -> None:
        '''Equivalent aware timestamps can identify the same UTC billing bucket.'''
        group = series(values={6: '1.25'})
        group['dataPoints'][5]['timestamp'] = '2026-10-06T02:00:00+02:00'
        spend = app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([group]))]),
                                    'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(spend.daily, Decimal('1.25'))

    def test_duplicate_group_and_duplicate_day_are_rejected(self) -> None:
        '''No overlapping series or day points are counted twice.'''
        for groups in ([series('A'), series('A')], [series()]):
            if len(groups) == 1:
                groups[0]['dataPoints'].append(copy.deepcopy(groups[0]['dataPoints'][0]))
            with self.subTest(count=len(groups)), self.assertRaisesRegex(app.ReporterError, 'repeated'):
                app.fetch_xai_costs(Session(posts=[RawResponse(xai_body(groups))]),
                                    'offline-key', 'team_offline', REPORT_DATE)

    def test_completeness_flag_and_single_usd_metric_are_required(self) -> None:
        '''Truncation, missing flags and missing/extra observations stop publication.'''
        for flag in (None, True, 0, 'false'):
            body = xai_body()
            body['limitReached'] = flag
            with self.subTest(flag=flag), self.assertRaises(app.ReporterError):
                app.fetch_xai_costs(Session(posts=[RawResponse(body)]), 'offline-key', 'team_offline', REPORT_DATE)
        body = xai_body()
        body.pop('limitReached')
        with self.assertRaises(app.ReporterError):
            app.fetch_xai_costs(Session(posts=[RawResponse(body)]), 'offline-key', 'team_offline', REPORT_DATE)
        for values in ([], ['1', '2'], [None], [True], ['1_000']):
            group = series()
            group['dataPoints'][5]['values'] = values
            with self.subTest(values=values), self.assertRaises(app.ReporterError):
                app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([group]))]),
                                    'offline-key', 'team_offline', REPORT_DATE)

    def test_missing_or_ambiguous_group_labels_are_rejected(self) -> None:
        '''Requested one-description grouping must have one nonempty label per series.'''
        for labels in (None, [], ['',], ['A', 'B'], [3]):
            group = series()
            group['groupLabels'] = labels
            with self.subTest(labels=labels), self.assertRaises(app.ReporterError):
                app.fetch_xai_costs(Session(posts=[RawResponse(xai_body([group]))]),
                                    'offline-key', 'team_offline', REPORT_DATE)


class ConfigurationAndTransportTests(unittest.TestCase):
    '''Preflight both providers, full UTC periods and sanitized read-only transport.'''

    def test_completed_utc_period_month_year_and_leap_boundaries(self) -> None:
        '''Month-to-date periods end at the next UTC midnight through calendar boundaries.'''
        for day, end in ((date(2024, 2, 29), date(2024, 3, 1)),
                         (date(2025, 12, 31), date(2026, 1, 1)),
                         (date(2026, 9, 30), date(2026, 10, 1)),
                         (date(2026, 10, 1), date(2026, 10, 2))):
            with self.subTest(day=day):
                start, ending = app.billing_period_utc(day)
                self.assertEqual(start, datetime(day.year, day.month, 1, tzinfo=timezone.utc))
                self.assertEqual(ending, datetime(end.year, end.month, end.day, tzinfo=timezone.utc))
                self.assertEqual((ending - start).days, day.day)
        for day in (date(1999, 12, 31), datetime.now(timezone.utc).date(),
                    datetime.now(timezone.utc).date() + timedelta(days=1),
                    datetime(2026, 10, 6, tzinfo=timezone.utc)):
            with self.subTest(day=day), self.assertRaises(app.ReporterError):
                app.billing_period_utc(day)

    def test_project_parser_is_explicit_deduplicated_and_bounded(self) -> None:
        '''An explicit malformed filter cannot fall back to organization scope.'''
        self.assertEqual(app.parse_openai_project_ids('  '), ())
        self.assertEqual(app.parse_openai_project_ids('proj_a, proj_b,proj_a'), ('proj_a', 'proj_b'))
        for raw in ('proj_a,', ',proj_a', 'proj_a,,proj_b', 'not_project', 'proj_a/x', 'proj_\u00e9'):
            with self.subTest(raw=raw), self.assertRaises(app.ReporterError):
                app.parse_openai_project_ids(raw)

    def test_all_configuration_is_validated_before_any_provider_request(self) -> None:
        '''A missing or unsafe credential or scope never allows even the first provider call.'''
        variations = [dict(ENVIRONMENT, **{key: ''}) for key in
                      ('OPENAI_ADMIN_KEY', 'XAI_MANAGEMENT_KEY', 'XAI_TEAM_ID')]
        variations += [dict(ENVIRONMENT, XAI_TEAM_ID='team/offline'),
                       dict(ENVIRONMENT, OPENAI_PROJECT_IDS='proj_a,')]
        variations += [dict(ENVIRONMENT, **{key: 'offline\nheader'}) for key in
                       ('OPENAI_ADMIN_KEY', 'XAI_MANAGEMENT_KEY')]
        variations += [dict(ENVIRONMENT, OPENAI_ADMIN_KEY=value) for value in
                       ('offline\rheader', 'offline\tkey', ' offline-key', 'offline key', 'offline-\u00e9')]
        for environment in variations:
            session = Mock()
            with self.subTest(changed=[key for key in environment if environment[key] != ENVIRONMENT[key]]), \
                    patch.dict('os.environ', environment, clear=True), self.assertRaises(app.ReporterError):
                app.get_live_report(session, REPORT_DATE)
            session.get.assert_not_called()
            session.post.assert_not_called()

    def test_configuration_representation_excludes_authentication(self) -> None:
        '''Debug representations include scope while omitting both mocked credential values.'''
        with patch.dict('os.environ', ENVIRONMENT, clear=True):
            configuration = app.validate_billing_configuration(REPORT_DATE)
        self.assertEqual(configuration.xai_team_id, 'team_offline')
        self.assertEqual(configuration.openai_project_ids, ())
        self.assertNotIn(ENVIRONMENT['OPENAI_ADMIN_KEY'], repr(configuration))
        self.assertNotIn(ENVIRONMENT['XAI_MANAGEMENT_KEY'], repr(configuration))

    def test_both_provider_successes_are_required_and_transport_is_safe(self) -> None:
        '''Only two successful complete reads yield a report; TLS remains verified without redirects.'''
        session = Session(gets=[RawResponse(openai_page())], posts=[RawResponse(xai_body())])
        with patch.dict('os.environ', ENVIRONMENT, clear=True):
            report = app.get_live_report(session, REPORT_DATE)
        self.assertFalse(session.trust_env)
        self.assertEqual(report.daily_total, Decimal('0'))
        self.assertEqual([method for method, _, _ in session.calls], ['GET', 'POST'])
        for _, _, kwargs in session.calls:
            self.assertIs(kwargs['verify'], True)
            self.assertIs(kwargs['allow_redirects'], False)
        session = Session(gets=[RawResponse(openai_page())], posts=[RawResponse({}, 403)])
        with patch.dict('os.environ', ENVIRONMENT, clear=True), self.assertRaisesRegex(app.ReporterError, 'HTTP 403'):
            app.get_live_report(session, REPORT_DATE)

    def test_network_errors_are_sanitized_without_chained_provider_details(self) -> None:
        '''Sensitive transport exception text cannot enter the report or user-facing error.'''
        for provider in ('OpenAI', 'xAI'):
            session = Mock()
            session.get.side_effect = requests.ConnectionError('offline-sensitive-marker')
            session.post.side_effect = requests.ConnectionError('offline-sensitive-marker')
            with self.subTest(provider=provider), self.assertRaises(app.ReporterError) as raised:
                if provider == 'OpenAI':
                    app.fetch_openai_costs(session, 'offline-key', REPORT_DATE)
                else:
                    app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
            self.assertNotIn('offline-sensitive-marker', str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)

    def test_retry_session_disables_inherited_environment_auth(self) -> None:
        '''The daily job's session cannot consult netrc or proxy environment configuration.'''
        with app.session_with_retry() as session:
            self.assertFalse(session.trust_env)
            retries = session.get_adapter(app.OPENAI_COSTS_URL).max_retries
            self.assertEqual(retries.allowed_methods, frozenset({'GET', 'POST'}))


if __name__ == '__main__':
    unittest.main()
