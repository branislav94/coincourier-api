'''Offline regressions for OpenAI source amounts and exact derived report values.'''

from __future__ import annotations

import copy
import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import app


REPORT_DATE = date(2026, 10, 10)
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    '''Use a complete UTC report day without relying on the execution clock.'''

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        '''Return the fixed observation instant in the requested timezone.'''
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class RawNumber:
    '''Mark an exact JSON numeric token that must not pass through Python float.'''

    def __init__(self, text: str) -> None:
        '''Retain synthetic numeric fixture text only.'''
        self.text = text


class RawResponse:
    '''Return raw-byte provider fixtures while forbidding Response.json float decoding.'''

    def __init__(self, content: bytes, status: int = 200) -> None:
        '''Store deterministic offline response bytes and status.'''
        self.content = content
        self.status_code = status

    def json(self) -> None:
        '''Fail if billing code bypasses its exact raw JSON parser.'''
        raise AssertionError('Use exact raw response bytes')


class Session:
    '''Keep independent request snapshots and return only queued synthetic responses.'''

    def __init__(self, responses: list[RawResponse]) -> None:
        '''Prepare the offline request queue without an HTTP transport.'''
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> RawResponse:
        '''Record one mocked costs GET.'''
        self.calls.append(('GET', url, copy.deepcopy(kwargs)))
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> RawResponse:
        '''Record one mocked xAI analytics read for provider-isolation proof.'''
        self.calls.append(('POST', url, copy.deepcopy(kwargs)))
        return self.responses.pop(0)


def page(days: tuple[int, ...] = tuple(range(1, 11)), *,
         amounts: dict[int, list[tuple[object, str, str | None]]] | None = None,
         more: bool = False, cursor: str | None = None) -> bytes:
    '''Encode official-shaped UTC buckets, retaining marked numeric tokens exactly.'''
    replacements = []
    buckets = []
    for day in days:
        start = int(datetime(2026, 10, day, tzinfo=timezone.utc).timestamp())
        results = []
        for value, label, project in (amounts or {}).get(day, []):
            if isinstance(value, RawNumber):
                placeholder = f'__raw_amount_{len(replacements)}__'
                replacements.append((placeholder, value.text))
                value = placeholder
            results.append({'object': 'organization.costs.result',
                            'amount': {'value': value, 'currency': 'usd'},
                            'line_item': label, 'project_id': project})
        buckets.append({'object': 'bucket', 'start_time': start, 'end_time': start + 86400,
                        'results': results})
    body = json.dumps({'object': 'page', 'data': buckets, 'has_more': more, 'next_page': cursor})
    for placeholder, token in replacements:
        body = body.replace(json.dumps(placeholder), token)
    return body.encode('utf-8')


class OpenAIAmountTests(unittest.TestCase):
    '''Accept bounded exact USD values and preserve accounting through every report boundary.'''

    def setUp(self) -> None:
        '''Install a fixed complete-day clock without reading any credentials.'''
        clock = patch.object(app, 'datetime', FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)

    def fetch(self, value: object) -> app.ProviderSpend:
        '''Exercise the actual raw JSON cost parser with one synthetic amount.'''
        session = Session([RawResponse(page(amounts={10: [(value, 'synthetic-cost', None)]}))])
        return app.fetch_openai_costs(session, 'offline-unit-key', REPORT_DATE)

    def test_raw_json_numbers_and_numeric_strings_accept_exact_supported_usd_formats(self) -> None:
        '''Zero, fractions, scientific notation, tiny values and refunds remain exact.'''
        texts = ('0', '-0', '0.000000', '0.125', '0.1234500', '1.25e-20',
                 '1e-300', '4.9406564584124654e-324', '1e-1024', '-0.000000001',
                 '1.123456789012345678901234567890123456789')
        for text in texts:
            for value in (text, RawNumber(text)):
                with self.subTest(token=text, encoding='number' if isinstance(value, RawNumber) else 'string'):
                    spend = self.fetch(value)
                    self.assertEqual(spend.daily, Decimal(text))
                    self.assertEqual(spend.month_to_date, Decimal(text))
                    self.assertEqual(spend.daily_breakdown['synthetic-cost'], Decimal(text))

    def test_equivalent_zero_and_long_trailing_zero_representations_are_accepted(self) -> None:
        '''Representational scale cannot reject a bounded zero or ordinary finite amount.'''
        for text, expected in (('0e300', '0'), ('0e-300', '0'), ('-0e-1024', '0'),
                               ('1.25' + '0' * 300, '1.25'), ('-1.250' + '0' * 300, '-1.25')):
            with self.subTest(encoding=text[:16]):
                self.assertEqual(self.fetch(RawNumber(text)).daily, Decimal(expected))
                self.assertEqual(self.fetch(text).daily, Decimal(expected))

    def test_legacy_supported_quantum_is_preserved_without_rounding(self) -> None:
        '''The newly accepted forms do not rewrite already supported source representations.'''
        for text in ('1.2300', '0.00012500', '-0.00500', '1.2500e-20'):
            with self.subTest(text=text):
                actual = app.openai_decimal_value(text, field_path='pages[0].data[0].results[0].amount.value')
                self.assertEqual(actual.as_tuple(), Decimal(text).as_tuple())

    def test_canonical_precision_accepts_long_zero_padding_but_text_remains_bounded(self) -> None:
        '''Source representation padding is harmless within its separate textual safety limit.'''
        text = '1.25' + '0' * 1100
        self.assertEqual(self.fetch(text).daily, Decimal('1.25'))
        self.assertEqual(self.fetch(RawNumber(text)).daily, Decimal('1.25'))
        observed = []
        with self.assertRaises(app.ReporterError) as raised:
            app.openai_decimal_value('1.' + '0' * 2048,
                                     field_path='pages[0].data[0].results[0].amount.value',
                                     observer=observed.append)
        self.assertEqual(raised.exception.code, 'invalid_usd_amount')
        self.assertEqual(observed[-1]['validation_rule'], 'numeric_text_length')

    def test_source_precision_fractional_scale_and_magnitude_have_explicit_boundaries(self) -> None:
        '''Valid edges remain exact, while unsupported source values fail instead of truncating.'''
        for value in ('0.' + '1' * 1024, '1e-1024', '9e256'):
            with self.subTest(edge=value[:20]):
                actual = app.openai_decimal_value(value, field_path='pages[0].data[0].results[0].amount.value')
                self.assertEqual(actual, Decimal(value))
        for value, rule in (('0.' + '1' * 1025, 'coefficient_precision'),
                            ('1e-1025', 'fractional_scale'), ('1e257', 'magnitude')):
            observed = []
            with self.subTest(rule=rule), self.assertRaises(app.ReporterError) as raised:
                app.openai_decimal_value(value, field_path='pages[0].data[0].results[0].amount.value',
                                         observer=observed.append)
            self.assertEqual(raised.exception.code, 'invalid_usd_amount')
            self.assertEqual(observed[-1]['validation_rule'], rule)

    def test_malformed_types_syntax_and_nonfinite_values_are_never_zero(self) -> None:
        '''No missing, boolean, binary-float, loose text or non-finite input becomes spend.'''
        for value in (None, True, False, 0.125, {}, [], ' 1', '1 ', '+1', '01', '.1', '1.',
                      '1_000', '\u0661.\u0662', 'not-a-number', 'NaN', 'Infinity', '-Infinity',
                      Decimal('NaN'), Decimal('sNaN'), Decimal('Infinity')):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(app.ReporterError) as raised:
                app.openai_decimal_value(value, field_path='pages[0].data[0].results[0].amount.value')
            self.assertEqual(raised.exception.code, 'invalid_usd_amount')

    def test_raw_json_ambiguity_remains_invalid_before_amount_validation(self) -> None:
        '''The additional amount formats do not relax JSON syntax or duplicate-key safety.'''
        for token in ('NaN', 'Infinity', '-Infinity', '01'):
            with self.subTest(token=token), self.assertRaises(app.ReporterError) as raised:
                self.fetch(RawNumber(token))
            self.assertEqual(raised.exception.code, 'invalid_json')
        raw = page(amounts={10: [('1', 'synthetic-cost', None)]})
        raw = raw.replace(b'"value": "1"', b'"value":"1","value":"2"')
        with self.assertRaises(app.ReporterError) as raised:
            app.fetch_openai_costs(Session([RawResponse(raw)]), 'offline-unit-key', REPORT_DATE)
        self.assertEqual(raised.exception.code, 'invalid_json')

    def test_paginated_daily_and_month_to_date_totals_are_exact_under_hostile_context(self) -> None:
        '''Raw costs and refunds aggregate across pages and dates without ambient rounding.'''
        first = page(tuple(range(1, 6)), amounts={1: [(RawNumber('1.25' + '0' * 300), 'prior', 'proj_unit')]},
                     more=True, cursor='unit-cursor')
        second = page(tuple(range(6, 11)), amounts={10: [
            (RawNumber('1e-1024'), 'tiny', 'proj_unit'), ('-0.005', 'refund', 'proj_unit'),
            ('0.010', 'usage', 'proj_unit'),
        ]})
        session = Session([RawResponse(first), RawResponse(second)])
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            spend = app.fetch_openai_costs(session, 'offline-unit-key', REPORT_DATE, ('proj_unit',))
        self.assertEqual(spend.daily, Decimal('0.005' + '0' * 1020 + '1'))
        self.assertEqual(spend.month_to_date, Decimal('1.255' + '0' * 1020 + '1'))
        self.assertEqual(spend.daily_breakdown['tiny'], Decimal('1e-1024'))
        self.assertEqual(session.calls[0][2]['params']['group_by[]'], ['line_item', 'project_id'])
        self.assertEqual(session.calls[0][2]['params']['project_ids[]'], ['proj_unit'])
        self.assertNotIn('page', session.calls[0][2]['params'])
        self.assertEqual(session.calls[1][2]['params']['page'], 'unit-cursor')
        self.assertTrue(all(call[2]['allow_redirects'] is False and call[2]['verify'] is True
                            for call in session.calls))

    def test_late_page_error_cannot_return_partial_totals(self) -> None:
        '''A valid tiny amount on page one cannot mask failed or incomplete pagination.'''
        first = page(tuple(range(1, 6)), amounts={1: [('1e-300', 'tiny', None)]},
                     more=True, cursor='unit-cursor')
        with self.assertRaises(app.ReporterError) as raised:
            app.fetch_openai_costs(Session([RawResponse(first), RawResponse(b'{}', 403)]),
                                   'offline-unit-key', REPORT_DATE)
        self.assertEqual(raised.exception.code, 'http_error')
        incomplete = page(tuple(range(6, 10)))
        with self.assertRaises(app.ReporterError) as raised:
            app.fetch_openai_costs(Session([RawResponse(first), RawResponse(incomplete)]),
                                   'offline-unit-key', REPORT_DATE)
        self.assertEqual(raised.exception.code, 'incomplete_coverage')

    def test_derived_budget_retains_maximum_mixed_scale_total_and_rejects_overflow(self) -> None:
        '''Accepted source extremes can be summed without losing the least-significant digit.'''
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            combined = app.exact_sum((Decimal('1e256'), Decimal('1e-1024')))
        expected = '1' + '0' * 256 + '.' + '0' * 1023 + '1'
        self.assertEqual(app.usd_text(combined), expected)
        self.assertEqual(len(combined.as_tuple().digits), 1281)
        with self.assertRaises(app.ReporterError):
            app.exact_sum((Decimal('9e256'), Decimal('9e256')))

    def test_snapshot_roundtrip_preserves_tiny_openai_values_and_extended_combined_total(self) -> None:
        '''Exact USD JSON keeps every fractional digit independently of display formatting.'''
        spend = self.fetch(RawNumber('1e-1024'))
        report = app.BillingReport(REPORT_DATE, spend, app.ProviderSpend(Decimal('1e256'), Decimal('1e256')), NOW)
        with localcontext() as context:
            context.prec = 2
            context.capitals = 0
            snapshot = app.report_to_dict(report)
            restored = app.report_from_dict(json.loads(json.dumps(snapshot)), REPORT_DATE)
        self.assertEqual(snapshot['providers']['openai']['daily_usd'], '0.' + '0' * 1023 + '1')
        self.assertEqual(snapshot['daily_total_usd'], '1' + '0' * 256 + '.' + '0' * 1023 + '1')
        self.assertEqual(restored.openai.daily, Decimal('1e-1024'))
        self.assertEqual(restored.daily_total, report.daily_total)

    def test_aggregated_openai_snapshot_uses_derived_precision_without_relaxing_source_precision(self) -> None:
        '''A valid aggregate wider than any single source record survives snapshot reuse.'''
        session = Session([RawResponse(page(amounts={10: [
            (RawNumber('1e256'), 'large', None), (RawNumber('1e-1024'), 'tiny', None),
        ]}))])
        spend = app.fetch_openai_costs(session, 'offline-unit-key', REPORT_DATE)
        report = app.BillingReport(REPORT_DATE, spend, app.ProviderSpend(Decimal('0'), Decimal('0')), NOW)
        snapshot = app.report_to_dict(report)
        restored = app.report_from_dict(json.loads(json.dumps(snapshot)), REPORT_DATE)
        expected = '1' + '0' * 256 + '.' + '0' * 1023 + '1'
        self.assertEqual(snapshot['providers']['openai']['daily_usd'], expected)
        self.assertEqual(restored.openai.daily, Decimal(expected))
        with self.assertRaises(app.ReporterError):
            app.openai_decimal_value(expected, field_path='pages[0].data[0].results[0].amount.value')

    def test_pdf_cents_presentation_does_not_round_the_exact_report_snapshot(self) -> None:
        '''PDF rendering rounds cents only, with the underlying provider totals left intact.'''
        spend = self.fetch(RawNumber('0.005' + '0' * 296 + '1'))
        report = app.BillingReport(REPORT_DATE, spend, app.ProviderSpend(Decimal('0'), Decimal('0')), NOW)
        document = Mock()
        with patch.object(app, 'SimpleDocTemplate', return_value=document), localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            app.create_pdf(report, Path('never-written-unit.pdf'))
        document.build.assert_called_once()
        texts = []
        for item in document.build.call_args.args[0]:
            if hasattr(item, 'getPlainText'):
                texts.append(item.getPlainText())
            if hasattr(item, '_cellvalues'):
                for row in item._cellvalues:
                    texts.extend(str(value) for value in row)
        self.assertIn('$0.01', ' '.join(texts))
        self.assertEqual(report.openai.daily, spend.daily)
        self.assertEqual(app.report_to_dict(report)['providers']['openai']['daily_usd'],
                         '0.005' + '0' * 296 + '1')

    def test_xai_source_validator_and_wire_query_remain_strict(self) -> None:
        '''The expanded OpenAI policy cannot relax xAI's established raw amount rules.'''
        with self.assertRaises(app.ReporterError):
            app.decimal_value('1e-300', 'xAI')
        body = {'limitReached': False, 'timeSeries': [{
            'groupLabels': ['synthetic-xai'],
            'dataPoints': [{'timestamp': f'2026-10-{day:02d}T00:00:00Z',
                            'values': ['1e-300' if day == 10 else '0']} for day in range(1, 11)],
        }]}
        session = Session([RawResponse(json.dumps(body).encode())])
        with self.assertRaises(app.ReporterError) as raised:
            app.fetch_xai_costs(session, 'offline-unit-key', 'team_unit', REPORT_DATE)
        self.assertEqual(raised.exception.code, 'invalid_usd_amount')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]['json']['analyticsRequest']['groupBy'], ['description'])
        self.assertEqual(session.calls[0][2]['json']['analyticsRequest']['values'],
                         [{'name': 'usd', 'aggregation': 'AGGREGATION_SUM'}])


if __name__ == '__main__':
    unittest.main()
