'''Offline zero-usage proof tests: grouped absence alone never establishes a zero cost.'''

from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import Mock

import requests

import app
from test_billing_correctness import REPORT_DATE, RawResponse, Session, series, xai_body


def aggregate(day: date = REPORT_DATE) -> dict[str, Any]:
    '''Create one explicit dense ungrouped USD zero series for the complete requested month.'''
    return {'groupLabels': [], 'dataPoints': [
        {'timestamp': f'{day.year:04d}-{day.month:02d}-{number:02d}T00:00:00Z', 'values': ['0']}
        for number in range(1, day.day + 1)
    ]}


class XaiZeroConfirmationTests(unittest.TestCase):
    '''Confirm zero only with explicit dense aggregate evidence on the same billing read scope.'''

    def read(self, confirmation: dict[str, Any] | bytes, day: date = REPORT_DATE) -> tuple[app.ProviderSpend, Session]:
        '''Serve grouped-empty followed by an offline confirmation response.'''
        session = Session(posts=[RawResponse(xai_body([])), RawResponse(confirmation)])
        spend = app.fetch_xai_costs(session, 'offline-key', 'team_offline', day)
        return spend, session

    def failure(self, confirmation: dict[str, Any] | bytes, code: str) -> app.ReporterError:
        '''Assert a confirmation cannot fabricate zero and return its safe diagnostic.'''
        with self.assertRaises(app.ReporterError) as raised:
            self.read(confirmation)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_empty_grouped_response_triggers_one_identical_ungrouped_confirmation(self) -> None:
        '''Only groupBy changes; endpoint, team, UTC interval, metric and safety settings match.'''
        spend, session = self.read(xai_body([aggregate()]))
        self.assertEqual(len(session.calls), 2)
        first, second = session.calls
        self.assertEqual(first[:2], second[:2])
        first_kwargs, second_kwargs = copy.deepcopy(first[2]), copy.deepcopy(second[2])
        self.assertEqual(first_kwargs['json']['analyticsRequest'].pop('groupBy'), ['description'])
        self.assertEqual(second_kwargs['json']['analyticsRequest'].pop('groupBy'), [])
        self.assertEqual(first_kwargs, second_kwargs)
        self.assertIs(second_kwargs['verify'], True)
        self.assertIs(second_kwargs['allow_redirects'], False)
        self.assertEqual(spend.daily, Decimal('0'))
        self.assertEqual(spend.month_to_date, Decimal('0'))
        self.assertEqual(spend.daily_breakdown, {})
        self.assertEqual(spend.accounting_evidence, app.XAI_ZERO_CONFIRMATION_EVIDENCE)

    def test_zero_metadata_aliases_are_empty_or_absent_only(self) -> None:
        '''The no-group tuple may be omitted, but any present alias must be an empty list.'''
        for labels in ({}, {'group': []}, {'groupLabels': []}, {'group': [], 'groupLabels': []}):
            group = aggregate()
            group.pop('groupLabels')
            group.update(labels)
            with self.subTest(labels=labels):
                spend, _ = self.read(xai_body([group]))
                self.assertTrue(spend.daily.is_zero())
        for labels in ({'groupLabels': None}, {'groupLabels': ''}, {'groupLabels': ['Grok']},
                       {'group': ['Grok']}, {'groupLabels': [], 'group': ['Grok']}):
            group = aggregate()
            group.update(labels)
            with self.subTest(labels=labels):
                self.failure(xai_body([group]), 'response_schema_invalid')

    def test_zero_confirmation_accepts_explicit_numeric_zero_forms(self) -> None:
        '''Finite signed and exponent zero representations are recorded zero observations.'''
        group = aggregate()
        for point, value in zip(group['dataPoints'], ('0', '-0.0', '0e2', '0.000', 0, '0.00')):
            point['values'] = [value]
        spend, _ = self.read(xai_body([group]))
        self.assertEqual(app.usd_text(spend.daily), '0')
        self.assertEqual(app.usd_text(spend.month_to_date), '0')

    def test_nonzero_confirmation_rejects_even_canceling_credits_or_zero_last_day(self) -> None:
        '''Each day must be zero; a net or daily zero cannot hide real recorded activity.'''
        for values in ({1: '1'}, {1: '1', 2: '-1'}, {6: '0.00000000000000000000001'}):
            group = aggregate()
            for day, value in values.items():
                group['dataPoints'][day - 1]['values'] = [value]
            with self.subTest(values=values):
                error = self.failure(xai_body([group]), 'aggregation_mismatch')
                self.assertEqual(error.failure_stage, 'coverage')
                self.assertEqual(error.details, {})

    def test_empty_confirmation_has_distinct_provisional_no_recorded_usage_state(self) -> None:
        '''Two empty collections supply no dense zero points or reconciled billing total.'''
        spend, session = self.read(xai_body([]))
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(spend.daily, Decimal('0'))
        self.assertEqual(spend.month_to_date, Decimal('0'))
        self.assertEqual(spend.daily_breakdown, {})
        self.assertEqual(spend.accounting_state, app.NO_RECORDED_USAGE)
        self.assertEqual(spend.accounting_evidence, app.XAI_EMPTY_USAGE_EVIDENCE)
        self.assertNotEqual(spend.accounting_evidence, app.XAI_ZERO_CONFIRMATION_EVIDENCE)
        self.assertEqual(spend.reconciliation_status, 'not_reconciled')
        self.assertIs(spend.allow_recheck, True)

    def test_confirmation_must_have_one_series_and_explicit_completeness(self) -> None:
        '''Multiple aggregate tuples, absent collection or completeness and truncation fail.'''
        for body, code in ((xai_body([aggregate(), aggregate()]), 'response_schema_invalid'),
                           ({'limitReached': False}, 'response_schema_invalid'),
                           ({'timeSeries': [aggregate()]}, 'response_schema_invalid'),
                           ({'limitReached': True, 'timeSeries': [aggregate()]}, 'response_truncated')):
            with self.subTest(code=code):
                self.failure(body, code)

    def test_sparse_duplicate_and_out_of_range_confirmation_days_fail(self) -> None:
        '''The confirmation follows all existing complete UTC-day invariants.'''
        group = aggregate()
        group['dataPoints'].pop(2)
        error = self.failure(xai_body([group]), 'incomplete_coverage')
        self.assertEqual(error.details['missing_utc_days'], ['2026-10-03'])
        group = aggregate()
        group['dataPoints'].append(copy.deepcopy(group['dataPoints'][0]))
        self.failure(xai_body([group]), 'duplicate_data')
        for timestamp in ('2026-10-07T00:00:00Z', '2026-10-06T23:59:59Z',
                          '2026-10-06T00:00:00.1Z'):
            group = aggregate()
            group['dataPoints'][5]['timestamp'] = timestamp
            with self.subTest(timestamp=timestamp):
                self.failure(xai_body([group]), 'out_of_range_day')

    def test_missing_or_invalid_confirmation_values_are_not_zero(self) -> None:
        '''No absent metric, extra metric, bool or malformed amount is inferred to be zero.'''
        for values, code in (([], 'response_schema_invalid'), (['0', '0'], 'response_schema_invalid'),
                             ([None], 'invalid_usd_amount'), ([True], 'invalid_usd_amount'),
                             (['NaN'], 'invalid_usd_amount'), (['1_000'], 'invalid_usd_amount')):
            group = aggregate()
            group['dataPoints'][5]['values'] = values
            with self.subTest(values=values):
                self.failure(xai_body([group]), code)
        self.failure(b'{"limitReached":false,"timeSeries":NaN}', 'invalid_json')

    def test_partial_malformed_or_truncated_grouped_response_never_falls_back(self) -> None:
        '''The secondary request cannot rescue an invalid populated primary response.'''
        sparse = series()
        sparse['dataPoints'].pop(0)
        cases = (xai_body([sparse]), xai_body([{'groupLabels': ['Grok'], 'dataPoints': []}]),
                 {'limitReached': True, 'timeSeries': []}, {'timeSeries': []},
                 {'limitReached': False}, {'limitReached': False, 'timeSeries': None}, b'{')
        for body in cases:
            session = Session(posts=[RawResponse(body), RawResponse(xai_body([aggregate()]))])
            with self.subTest(body=body), self.assertRaises(app.ReporterError):
                app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
            self.assertEqual(len(session.calls), 1)

    def test_http_and_network_failures_never_trigger_further_queries(self) -> None:
        '''Denied primary reads and failed confirmation reads preserve their real failure.'''
        for responses, count, code in (([RawResponse({}, 401)], 1, 'http_error'),
                                      ([RawResponse(xai_body([])), RawResponse({}, 403)], 2, 'http_error'),
                                      ([requests.ConnectionError('offline-private-marker')], 1, 'network_error'),
                                      ([RawResponse(xai_body([])), requests.ConnectionError('offline-private-marker')],
                                       2, 'network_error')):
            session = Mock()
            session.post.side_effect = responses
            with self.subTest(code=code, count=count), self.assertRaises(app.ReporterError) as raised:
                app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
            self.assertEqual(session.post.call_count, count)
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn('offline-private-marker', str(raised.exception))

    def test_normal_grouped_costs_still_use_one_query_without_confirmation_evidence(self) -> None:
        '''The successful preexisting grouped calculation and breakdown are preserved.'''
        session = Session(posts=[RawResponse(xai_body([series(values={5: '2.22', 6: '1.1111'})]))])
        spend = app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(spend.daily, Decimal('1.1111'))
        self.assertEqual(spend.month_to_date, Decimal('3.3311'))
        self.assertEqual(spend.daily_breakdown, {'Grok': Decimal('1.1111')})
        self.assertIsNone(spend.accounting_evidence)
        self.assertEqual(len(session.calls), 1)

    def test_leap_and_month_end_confirmation_requires_every_requested_day(self) -> None:
        '''Dense zero proof respects complete month-to-date calendar boundaries.'''
        for day, end in ((date(2024, 2, 29), '2024-03-01 00:00:00'),
                         (date(2025, 12, 31), '2026-01-01 00:00:00')):
            with self.subTest(day=day):
                spend, session = self.read(xai_body([aggregate(day)]), day)
                self.assertEqual(spend.daily, Decimal('0'))
                self.assertEqual(session.calls[1][2]['json']['analyticsRequest']['timeRange']['endTime'], end)


class ZeroEvidenceSnapshotTests(unittest.TestCase):
    '''Retain proof on retries without changing legacy snapshots or labeling synthetic data as real.'''

    def report(self, evidence: str | None = app.XAI_ZERO_CONFIRMATION_EVIDENCE) -> app.BillingReport:
        '''Build an in-memory report fixture without generating a file or PDF.'''
        return app.BillingReport(REPORT_DATE, app.ProviderSpend(Decimal('1'), Decimal('2')),
                                 app.ProviderSpend(Decimal('0'), Decimal('0'), {}, evidence),
                                 datetime(2026, 10, 7, tzinfo=timezone.utc))

    def test_verified_zero_evidence_round_trips_through_snapshot(self) -> None:
        '''A reused snapshot preserves the explicit zero confirmation provenance.'''
        data = app.report_to_dict(self.report())
        self.assertEqual(data['providers']['xai']['accounting_evidence'], app.XAI_ZERO_CONFIRMATION_EVIDENCE)
        self.assertNotIn('accounting_evidence', data['providers']['openai'])
        restored = app.report_from_dict(data, REPORT_DATE)
        self.assertEqual(restored.xai.accounting_evidence, app.XAI_ZERO_CONFIRMATION_EVIDENCE)
        self.assertEqual(restored.xai.daily_breakdown, {})

    def test_normal_and_demo_snapshots_omit_optional_provenance(self) -> None:
        '''Legacy snapshot shape and synthetic labeling remain unchanged.'''
        data = app.report_to_dict(self.report(None))
        self.assertNotIn('accounting_evidence', data['providers']['xai'])
        self.assertIsNone(app.report_from_dict(data, REPORT_DATE).xai.accounting_evidence)
        report = self.report()
        demo = app.BillingReport(report.report_date, report.openai, report.xai, report.generated_at, demo=True)
        data = app.report_to_dict(demo)
        self.assertEqual(data['data_type'], 'SYNTHETIC_DEMO')
        self.assertNotIn('accounting_evidence', data['providers']['xai'])

    def test_unknown_null_or_nonzero_cached_zero_evidence_is_rejected(self) -> None:
        '''A provenance flag cannot override stored accounting data or unknown proof values.'''
        for change in ({'accounting_evidence': 'offline-unknown-marker'}, {'accounting_evidence': None},
                       {'daily_usd': '1'}, {'month_to_date_usd': '-1'},
                       {'daily_breakdown_usd': {'Invented': '0'}}):
            data = app.report_to_dict(self.report())
            data['providers']['xai'].update(change)
            with self.subTest(change=change), self.assertRaises(app.ReporterError) as raised:
                app.report_from_dict(data, REPORT_DATE)
            self.assertEqual(raised.exception.code, 'response_schema_invalid')

    def test_xai_zero_proof_cannot_be_attached_to_openai(self) -> None:
        '''The cached proof is bound to the provider that performed the secondary read.'''
        data = app.report_to_dict(self.report(None))
        data['providers']['openai'] = {'daily_usd': '0', 'month_to_date_usd': '0',
                                      'daily_breakdown_usd': {},
                                      'accounting_evidence': app.XAI_ZERO_CONFIRMATION_EVIDENCE}
        with self.assertRaises(app.ReporterError):
            app.report_from_dict(data, REPORT_DATE)


if __name__ == '__main__':
    unittest.main()
