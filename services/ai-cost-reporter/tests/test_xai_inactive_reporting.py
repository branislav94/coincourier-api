'''Offline inactive accounting state tests with mocked PDF layout and no report generation.'''

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import app
from test_billing_correctness import REPORT_DATE, RawResponse, Session, series, xai_body
from test_xai_zero_usage import aggregate


def inactive_spend() -> app.ProviderSpend:
    '''Create the exact provisional absence-of-records metadata returned by the reader.'''
    return app.ProviderSpend(
        Decimal('0'), Decimal('0'), {},
        accounting_evidence=app.XAI_EMPTY_USAGE_EVIDENCE,
        accounting_state=app.NO_RECORDED_USAGE,
        reconciliation_status='not_reconciled', allow_recheck=True,
    )


def report(spend: app.ProviderSpend | None = None, demo: bool = False) -> app.BillingReport:
    '''Build a fixture in memory, never querying APIs or writing report artifacts.'''
    return app.BillingReport(REPORT_DATE, app.ProviderSpend(Decimal('1.23'), Decimal('4.56')),
                             spend or inactive_spend(), datetime(2026, 10, 7, tzinfo=timezone.utc), demo=demo)


class InactiveReaderTests(unittest.TestCase):
    '''Require two valid explicitly empty responses from the same immutable query scope.'''

    def test_two_exact_empty_responses_produce_provisional_recorded_zero_without_daily_points(self) -> None:
        '''No point, label or dense coverage is manufactured for an absence-of-records state.'''
        session = Session(posts=[RawResponse(xai_body([])), RawResponse(xai_body([]))])
        spend = app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(spend, inactive_spend())
        self.assertEqual(len(session.calls), 2)
        first, second = copy.deepcopy(session.calls)
        self.assertEqual(first[:2], second[:2])
        self.assertEqual(first[2]['json']['analyticsRequest'].pop('groupBy'), ['description'])
        self.assertEqual(second[2]['json']['analyticsRequest'].pop('groupBy'), [])
        self.assertEqual(first[2], second[2])
        self.assertEqual(spend.daily_breakdown, {})
        self.assertEqual(spend.reconciliation_status, 'not_reconciled')
        self.assertIs(spend.allow_recheck, True)

    def test_unexpected_empty_response_metadata_is_rejected_on_either_attempt(self) -> None:
        '''Unknown scope, error or paging fields cannot be interpreted as no recorded usage.'''
        for attempt in (0, 1):
            for key, value in (('teamId', 'other-team'), ('error', 'offline-private-marker'),
                               ('next_page', 'cursor'), ('has_more', True), ('startTime', 'wrong-period'),
                               ('extra', None)):
                bodies = [xai_body([]), xai_body([])]
                bodies[attempt][key] = value
                session = Session(posts=[RawResponse(body) for body in bodies])
                with self.subTest(attempt=attempt, key=key), self.assertRaises(app.ReporterError) as raised:
                    app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
                self.assertEqual(raised.exception.code, 'response_schema_invalid')
                self.assertEqual(raised.exception.details, {})
                self.assertNotIn('offline-private-marker', str(raised.exception))
                self.assertNotIn(key, str(raised.exception))
                self.assertEqual(len(session.calls), attempt + 1)

    def test_missing_flag_missing_collection_or_truncation_never_becomes_inactive(self) -> None:
        '''An incomplete or malformed response is still an error, including after an empty primary.'''
        malformed = ({'timeSeries': []}, {'limitReached': False},
                     {'timeSeries': None, 'limitReached': False},
                     {'timeSeries': [], 'limitReached': True})
        for first_empty in (False, True):
            for body in malformed:
                bodies = ([xai_body([])] if first_empty else []) + [body]
                session = Session(posts=[RawResponse(value) for value in bodies])
                with self.subTest(first_empty=first_empty, body=body), self.assertRaises(app.ReporterError):
                    app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)

    def test_grouped_nonzero_and_dense_explicit_zero_keep_recorded_state(self) -> None:
        '''Populated records use the existing calculations and never claim absent-record provenance.'''
        session = Session(posts=[RawResponse(xai_body([series(values={6: '1.23456789'})]))])
        normal = app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(normal.accounting_state, app.RECORDED_SPENDING)
        self.assertEqual(normal.daily, Decimal('1.23456789'))
        self.assertIsNone(normal.accounting_evidence)
        self.assertIsNone(normal.reconciliation_status)
        self.assertIsNone(normal.allow_recheck)
        session = Session(posts=[RawResponse(xai_body([])), RawResponse(xai_body([aggregate()]))])
        zero = app.fetch_xai_costs(session, 'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(zero.accounting_state, app.RECORDED_SPENDING)
        self.assertEqual(zero.accounting_evidence, app.XAI_ZERO_CONFIRMATION_EVIDENCE)
        self.assertIsNone(zero.reconciliation_status)

    def test_later_records_are_fresh_reads_and_do_not_modify_saved_snapshot(self) -> None:
        '''Rechecking the same UTC period reveals delayed spend without rewriting prior evidence.'''
        initial = Session(posts=[RawResponse(xai_body([])), RawResponse(xai_body([]))])
        inactive = app.fetch_xai_costs(initial, 'offline-key', 'team_offline', REPORT_DATE)
        saved = app.report_to_dict(report(inactive))
        original = copy.deepcopy(saved)
        later = Session(posts=[RawResponse(xai_body([series(values={5: '2.5', 6: '1.25'})]))])
        updated = app.fetch_xai_costs(later, 'offline-key', 'team_offline', REPORT_DATE)
        self.assertEqual(updated.accounting_state, app.RECORDED_SPENDING)
        self.assertEqual(updated.daily, Decimal('1.25'))
        self.assertEqual(updated.month_to_date, Decimal('3.75'))
        self.assertEqual(initial.calls[0][2]['json']['analyticsRequest']['timeRange'],
                         later.calls[0][2]['json']['analyticsRequest']['timeRange'])
        self.assertEqual(saved, original)
        restored = app.report_from_dict(saved, REPORT_DATE)
        self.assertEqual(restored.xai.accounting_state, app.NO_RECORDED_USAGE)


class InactiveSnapshotAndPresentationTests(unittest.TestCase):
    '''Validate persisted inactive state and qualified PDF text without creating a PDF or directory.'''

    def test_inactive_snapshot_round_trip_and_notes_preserve_recheck_qualification(self) -> None:
        '''Provisional absence is explicit while recorded numeric totals remain useful.'''
        data = app.report_to_dict(report())
        self.assertEqual(data['providers']['xai'], {
            'daily_usd': '0', 'month_to_date_usd': '0', 'daily_breakdown_usd': {},
            'accounting_evidence': app.XAI_EMPTY_USAGE_EVIDENCE,
            'accounting_state': app.NO_RECORDED_USAGE,
            'reconciliation_status': 'not_reconciled', 'allow_recheck': True,
        })
        self.assertEqual(data['daily_total_usd'], '1.23')
        self.assertEqual(data['month_to_date_total_usd'], '4.56')
        self.assertIn('No recorded usage', data['notes'])
        self.assertIn('Not reconciled', data['notes'])
        self.assertIn('late billing', data['notes'])
        self.assertIn('same completed UTC period', data['notes'])
        self.assertEqual(app.report_from_dict(data, REPORT_DATE).xai, inactive_spend())

    def test_inactive_cached_metadata_requires_exact_fields_and_consistent_values(self) -> None:
        '''No cached state can override nonzero costs, labels, invalid flags or unsupported metadata.'''
        changes = ({'daily_usd': '1'}, {'month_to_date_usd': '-1'},
                   {'daily_breakdown_usd': {'Invented zero day': '0'}},
                   {'accounting_state': app.RECORDED_SPENDING}, {'accounting_state': 'UNKNOWN'},
                   {'accounting_evidence': app.XAI_ZERO_CONFIRMATION_EVIDENCE},
                   {'accounting_evidence': None}, {'reconciliation_status': 'reconciled'},
                   {'allow_recheck': False}, {'allow_recheck': 1}, {'allow_recheck': 'true'},
                   {'scope': 'offline-private-marker'})
        for change in changes:
            data = app.report_to_dict(report())
            data['providers']['xai'].update(change)
            with self.subTest(change=change), self.assertRaises(app.ReporterError) as raised:
                app.report_from_dict(data, REPORT_DATE)
            self.assertEqual(raised.exception.code, 'response_schema_invalid')
        for key in ('accounting_state', 'accounting_evidence', 'reconciliation_status', 'allow_recheck'):
            data = app.report_to_dict(report())
            data['providers']['xai'].pop(key)
            with self.subTest(missing=key), self.assertRaises(app.ReporterError):
                app.report_from_dict(data, REPORT_DATE)

    def test_inactive_cached_evidence_is_bound_to_xai_and_report_date(self) -> None:
        '''An inactive record cannot be assigned to another provider or UTC snapshot date.'''
        data = app.report_to_dict(report())
        data['providers']['openai'] = copy.deepcopy(data['providers']['xai'])
        with self.assertRaises(app.ReporterError):
            app.report_from_dict(data, REPORT_DATE)
        data = app.report_to_dict(report())
        data['report_date_utc'] = '2026-10-05'
        with self.assertRaises(app.ReporterError):
            app.report_from_dict(data, REPORT_DATE)

    def test_normal_and_demo_snapshot_schema_and_notes_are_unchanged(self) -> None:
        '''Ordinary records and synthetic reports omit inactive provenance and flags.'''
        normal = report(app.ProviderSpend(Decimal('1'), Decimal('2')))
        data = app.report_to_dict(normal)
        self.assertEqual(set(data['providers']['xai']), {'daily_usd', 'month_to_date_usd', 'daily_breakdown_usd'})
        self.assertEqual(data['notes'], app.NOTES)
        demo = app.report_to_dict(report(demo=True))
        self.assertEqual(demo['data_type'], 'SYNTHETIC_DEMO')
        self.assertEqual(set(demo['providers']['xai']), {'daily_usd', 'month_to_date_usd', 'daily_breakdown_usd'})
        self.assertEqual(demo['notes'], app.NOTES)

    def test_email_plain_and_html_carry_inactive_qualification_without_network_or_file_reads(self) -> None:
        '''Recipients see the provisional state in either body without opening the attached PDF.'''
        environment = {
            'EMAIL_TO': 'recipient@example.invalid', 'SMTP_HOST': 'smtp.example.invalid',
            'SMTP_FROM': 'reporter@example.invalid', 'SMTP_SECURITY': 'starttls',
        }
        attachment = Mock(spec=Path)
        attachment.name = 'offline-mocked.pdf'
        attachment.read_bytes.return_value = b'%PDF-mocked-in-memory-attachment'
        with patch.dict('os.environ', environment, clear=True), \
                patch('app.smtplib.SMTP') as smtp_factory, \
                patch('app.ssl.create_default_context', return_value=Mock()):
            app.send_email(report(), attachment)
        relay = smtp_factory.return_value.__enter__.return_value
        relay.send_message.assert_called_once()
        relay.login.assert_not_called()
        message = relay.send_message.call_args.args[0]
        for kind in ('plain', 'html'):
            body = message.get_body(preferencelist=(kind,)).get_content()
            with self.subTest(kind=kind):
                self.assertIn('No recorded usage', body)
                self.assertIn('Recorded spend is $0.00', body)
                self.assertIn('no provider billing records', body)
                self.assertIn('Not reconciled', body)
                self.assertIn('late billing', body)
                self.assertIn('same completed UTC period', body)
                self.assertIn('delivered snapshots stay unchanged', body)
        attachment.read_bytes.assert_called_once_with()

    def test_pdf_story_marks_recorded_zero_unreconciled_and_does_not_invent_xai_line_items(self) -> None:
        '''Capture the layout in memory; intercept both the output directory and PDF writer.'''
        output = Mock(spec=Path)
        document = Mock()
        with patch('app.SimpleDocTemplate', return_value=document):
            app.create_pdf(report(), output)
        document.build.assert_called_once()
        story = document.build.call_args.args[0]

        def text(item: Any) -> str:
            '''Read mock layout objects without rendering or writing a document.'''
            if isinstance(item, str):
                return item
            if isinstance(item, (list, tuple)):
                return ' '.join(text(value) for value in item)
            if hasattr(item, 'getPlainText'):
                return item.getPlainText()
            if hasattr(item, '_cellvalues'):
                return text(item._cellvalues)
            if hasattr(item, '_content'):
                return text(item._content)
            return ''

        visible = text(story)
        self.assertIn('xAI / Grok (no recorded usage)', visible)
        self.assertIn('RECORDED TOTAL', visible)
        self.assertIn('Recorded spend is $0.00', visible)
        self.assertIn('no provider billing records', visible)
        self.assertIn('Not reconciled', visible)
        self.assertIn('late billing', visible)
        self.assertIn('same completed UTC period', visible)
        self.assertIn('delivered snapshots stay unchanged', visible)
        self.assertEqual(sum(isinstance(item, app.KeepTogether) for item in story), 2)
        xai_section = next(item for item in story if isinstance(item, app.KeepTogether)
                           and 'xAI / Grok - daily usage descriptions' in text(item))
        self.assertFalse(any(isinstance(item, app.Table) for item in xai_section._content))


if __name__ == '__main__':
    unittest.main()
