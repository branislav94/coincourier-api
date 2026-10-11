'''Offline checks for bounded, private OpenAI amount validation diagnostics.'''

from __future__ import annotations

import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import requests

import app
from test_openai_amounts import FixedDatetime, REPORT_DATE, RawNumber, RawResponse, Session, page


PRIVATE = 'unit-private-amount-details-must-stay-hidden'
FIELD_PATH = 'pages[0].data[0].results[0].amount.value'
METADATA_FIELDS = {
    'numeric_type', 'decimal_digit_count', 'exponent', 'adjusted_exponent', 'finite',
    'validation_rule', 'legacy_validation_rule', 'response_field_path',
}
SERVICE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('openai_amount_diagnostic_helper',
                                            SERVICE_ROOT / 'scripts' / 'billing_smoke_test.py')
if SPEC is None or SPEC.loader is None:
    raise RuntimeError('Billing diagnostic helper could not be loaded')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class AmountMetadataTests(unittest.TestCase):
    '''Validation evidence exposes bounded shapes and fixed rules instead of monetary input.'''

    def setUp(self) -> None:
        '''Fix the complete UTC day without reading process billing configuration.'''
        clock = patch.object(app, 'datetime', FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)

    def test_observer_reports_only_exact_allowlisted_metadata_keys(self) -> None:
        '''Successful source validation can be inspected without storing its input or label.'''
        for value, kind in ((Decimal('1e-300'), 'decimal'), (1, 'integer'), ('1.2500', 'string')):
            observed = []
            with self.subTest(kind=kind):
                result = app.openai_decimal_value(value, field_path=FIELD_PATH, observer=observed.append)
                self.assertEqual(result, Decimal(str(value)))
                self.assertEqual(len(observed), 1)
                metadata = observed[0]
                self.assertEqual(set(metadata), METADATA_FIELDS)
                self.assertEqual(metadata['numeric_type'], kind)
                self.assertEqual(metadata['validation_rule'], 'accepted')
                self.assertEqual(metadata['response_field_path'], FIELD_PATH)
                self.assertIs(metadata['finite'], True)
                self.assertIsInstance(metadata['decimal_digit_count'], int)
                self.assertNotIn('raw_value', metadata)
                self.assertNotIn('value', metadata)

    def test_rejected_amount_type_or_syntax_has_shape_evidence_without_input_text(self) -> None:
        '''Malformed values are described by fixed type/rule enums rather than echoed data.'''
        for value, kind, rule in ((None, 'null', 'numeric_type'), (True, 'boolean', 'numeric_type'),
                                  (0.125, 'float', 'numeric_type'), ({}, 'other', 'numeric_type'),
                                  (PRIVATE, 'string', 'numeric_syntax')):
            observed = []
            with self.subTest(kind=kind), self.assertRaises(app.ReporterError) as raised:
                app.openai_decimal_value(value, field_path=FIELD_PATH, observer=observed.append)
            metadata = raised.exception.details['amount_metadata']
            self.assertEqual(metadata, observed[-1])
            self.assertEqual(metadata['numeric_type'], kind)
            self.assertEqual(metadata['validation_rule'], rule)
            self.assertNotIn(PRIVATE, json.dumps(metadata))
            self.assertNotIn(PRIVATE, str(raised.exception))

    def test_newly_supported_values_identify_their_previous_bound_rule(self) -> None:
        '''The old rejection is explained while accepted small values remain unrounded.'''
        for value in ('1e-300', '1e-1024', '1.25' + '0' * 300, '0e300'):
            observed = []
            with self.subTest(value_prefix=value[:16]):
                app.openai_decimal_value(value, field_path=FIELD_PATH, observer=observed.append)
                self.assertEqual(observed[-1]['validation_rule'], 'accepted')
                self.assertEqual(observed[-1]['legacy_validation_rule'], 'decimal_exponent')

    def test_paginated_observer_paths_locate_records_without_project_ids_or_labels(self) -> None:
        '''Diagnostic paths identify structural positions without copying sensitive record metadata.'''
        first = page(tuple(range(1, 6)), amounts={1: [(RawNumber('1e-300'), PRIVATE, 'proj_private')]},
                     more=True, cursor=PRIVATE)
        second = page(tuple(range(6, 11)), amounts={10: [('1.25', PRIVATE, 'proj_private')]})
        observed = []
        result = app.fetch_openai_costs(Session([RawResponse(first), RawResponse(second)]),
                                       'unit-private-key', REPORT_DATE, ('proj_private',),
                                       amount_observer=observed.append)
        self.assertEqual(result.daily, Decimal('1.25'))
        self.assertEqual(len(observed), 2)
        paths = [metadata['response_field_path'] for metadata in observed]
        for path in paths:
            self.assertRegex(path, r'^pages\[[0-9]+\]\.data\[[0-9]+\]\.results\[[0-9]+\]\.amount\.value$')
        self.assertNotEqual(paths[0].split('.data')[0], paths[1].split('.data')[0])
        serialized = json.dumps(observed)
        self.assertNotIn(PRIVATE, serialized)
        self.assertNotIn('proj_private', serialized)
        self.assertNotIn('unit-private-key', serialized)

    def test_failed_late_record_keeps_its_path_and_never_discloses_raw_amount(self) -> None:
        '''An amount error after valid records remains actionable without exposing the record.'''
        raw = page(amounts={10: [('1', 'valid', None), (PRIVATE, PRIVATE, None)]})
        observed = []
        with self.assertRaises(app.ReporterError) as raised:
            app.fetch_openai_costs(Session([RawResponse(raw)]), 'unit-private-key', REPORT_DATE,
                                   amount_observer=observed.append)
        self.assertEqual(len(observed), 2)
        metadata = raised.exception.details['amount_metadata']
        self.assertEqual(metadata, observed[-1])
        self.assertTrue(metadata['response_field_path'].endswith('.results[1].amount.value'))
        self.assertEqual(metadata['validation_rule'], 'numeric_syntax')
        self.assertNotIn(PRIVATE, json.dumps(raised.exception.details))


class DiagnosticResponse(RawResponse):
    '''Supply exact bytes through the real guarded session without HTTP connections.'''

    def __init__(self, content: bytes, status: int = 200) -> None:
        '''Keep synthetic headers available only to the mocked transport.'''
        super().__init__(content, status)
        self.headers = {'Content-Type': 'application/json', 'X-Unit-Private': PRIVATE}
        self.closed = False

    def close(self) -> None:
        '''Record cleanup without opening any external resource.'''
        self.closed = True


class OpenAIAmountDiagnosticTests(unittest.TestCase):
    '''Numeric diagnostics stay opt-in, bounded and independent of provider raw contents.'''

    def setUp(self) -> None:
        '''Use synthetic process configuration and a fixed clock only.'''
        environment = patch.dict(os.environ, {'OPENAI_ADMIN_KEY': 'unit-private-openai-key',
                                             'OPENAI_PROJECT_IDS': ''}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for module in (app, smoke):
            clock = patch.object(module, 'datetime', FixedDatetime)
            clock.start()
            self.addCleanup(clock.stop)

    def run_main(self, arguments: list[str]) -> tuple[int, dict[str, Any] | None, str]:
        '''Capture safe structured output and verify that private fixture values never escape.'''
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = smoke.main(arguments)
        rendered = output.getvalue() + errors.getvalue()
        for private in (PRIVATE, 'unit-private-openai-key', 'proj_private'):
            self.assertNotIn(private, rendered)
        self.assertNotIn('Traceback', rendered)
        body = json.loads(output.getvalue()) if output.getvalue() else None
        return code, body, rendered

    @staticmethod
    def prohibit_billing_environment(name: str, default: Any = None) -> Any:
        '''Allow argparse locale defaults while refusing every application environment lookup.'''
        if name in {'LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'LANG'}:
            return default
        raise AssertionError('Offline validation cannot inspect application environment')

    def run_mocked(self, responses: list[DiagnosticResponse | Exception], *, metadata: bool = True,
                   provider: str = 'openai') -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        '''Exercise provider parsing and diagnostic formatting through a fake requests transport.'''
        calls = []

        def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> DiagnosticResponse:
            '''Assert that the guarded session forwards credentials only to allowed read endpoints.'''
            self.assertFalse(session.trust_env)
            self.assertIsNone(session.auth)
            self.assertEqual(len(session.cookies), 0)
            self.assertIs(kwargs.get('verify'), True)
            self.assertIs(kwargs.get('allow_redirects'), False)
            calls.append({'method': method, 'url': url, 'options': kwargs})
            if not responses:
                raise AssertionError('Unexpected provider request')
            reply = responses.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        arguments = ['--diagnostic', '--live', '--provider', provider, '--date', '2026-10-10']
        if metadata:
            arguments.append('--amount-metadata')
        with patch.object(requests.Session, 'request', autospec=True, side_effect=request):
            code, body, _ = self.run_main(arguments)
        self.assertEqual(responses, [], 'All expected reads must be consumed')
        self.assertIsInstance(body, dict)
        assert body is not None
        self.assertFalse(body['files_written'])
        self.assertFalse(body['delivery_performed'])
        return code, body, calls

    def test_offline_amount_metadata_plan_reads_no_environment_and_opens_no_session(self) -> None:
        '''The new flag describes shapes without fetching credentials or provider records.'''
        with patch.object(os.environ, 'get', side_effect=self.prohibit_billing_environment), \
                patch.object(requests.Session, 'request', side_effect=AssertionError('No network')):
            code, result, _ = self.run_main(['--diagnostic', '--amount-metadata', '--provider', 'openai',
                                           '--date', '2026-10-10'])
        self.assertEqual(code, 0)
        assert result is not None
        self.assertEqual(result['status'], 'prepared_offline')
        self.assertFalse(result['network_requests'])
        self.assertEqual(result['required_environment'], ['OPENAI_ADMIN_KEY'])
        self.assertEqual(result['numeric_diagnostics']['provider'], 'openai')
        self.assertTrue(result['numeric_diagnostics']['metadata_only'])
        self.assertEqual(result['numeric_diagnostics']['max_normal_samples'], 16)
        self.assertEqual(result['numeric_diagnostics']['max_flagged_samples'], 64)

    def test_invalid_flag_combinations_fail_before_environment_or_transport(self) -> None:
        '''Numeric shape inspection cannot be selected without OpenAI diagnostics.'''
        for arguments in (['--amount-metadata', '--live'],
                          ['--diagnostic', '--amount-metadata', '--provider', 'xai', '--live']):
            with self.subTest(arguments=arguments), \
                    patch.object(os.environ, 'get', side_effect=self.prohibit_billing_environment), \
                    patch.object(requests.Session, 'request', side_effect=AssertionError('No network')):
                code, _result, _ = self.run_main(arguments + ['--date', '2026-10-10'])
                self.assertEqual(code, 1)

    def test_success_without_opt_in_does_not_collect_numeric_metadata(self) -> None:
        '''The existing successful diagnostic response keeps numeric samples absent by default.'''
        code, result, calls = self.run_mocked(
            [DiagnosticResponse(page(amounts={10: [(RawNumber('1e-300'), PRIVATE, None)]}))],
            metadata=False,
        )
        self.assertEqual(code, 0)
        check = result['checks']['openai_costs']
        self.assertEqual(check['daily_usd'], '0.' + '0' * 299 + '1')
        self.assertNotIn('observed_amount_count', check)
        self.assertNotIn('amount_metadata', check)
        self.assertEqual(len(calls), 1)

    def test_opt_in_success_reports_raw_numeric_shapes_and_legacy_rule_without_labels(self) -> None:
        '''An accepted high-scale value explains the earlier guard without exposing its source record.'''
        code, result, _ = self.run_mocked([DiagnosticResponse(page(amounts={10: [
            (RawNumber('1e-300'), PRIVATE, None), ('1.2500', PRIVATE + '-normal', None),
        ]}))])
        self.assertEqual(code, 0)
        check = result['checks']['openai_costs']
        self.assertEqual(check['observed_amount_count'], 2)
        self.assertFalse(check['amount_metadata_truncated'])
        metadata = check['amount_metadata']
        self.assertEqual(len(metadata), 2)
        self.assertTrue(all(set(sample) == METADATA_FIELDS for sample in metadata))
        tiny = next(sample for sample in metadata if sample['exponent'] == -300)
        self.assertEqual(tiny['numeric_type'], 'decimal')
        self.assertEqual(tiny['validation_rule'], 'accepted')
        self.assertEqual(tiny['legacy_validation_rule'], 'decimal_exponent')
        self.assertEqual(tiny['decimal_digit_count'], 1)
        self.assertEqual(tiny['adjusted_exponent'], -300)
        self.assertNotIn(PRIVATE, json.dumps(metadata))

    def test_flagged_samples_are_reserved_and_collection_is_bounded(self) -> None:
        '''Normal rows cannot crowd out legacy-policy evidence, and large pages cannot grow output.'''
        records = [('1', f'normal-{index}', None) for index in range(20)]
        records += [(RawNumber('1e-300'), f'legacy-{index}', None) for index in range(70)]
        code, result, _ = self.run_mocked([DiagnosticResponse(page(amounts={10: records}))])
        self.assertEqual(code, 0)
        check = result['checks']['openai_costs']
        self.assertEqual(check['observed_amount_count'], 90)
        self.assertTrue(check['amount_metadata_truncated'])
        self.assertEqual(len(check['amount_metadata']), 80)
        self.assertEqual(sum(sample['legacy_validation_rule'] is not None
                             for sample in check['amount_metadata']), 64)
        self.assertTrue(all(sample['legacy_validation_rule'] is not None
                            for sample in check['amount_metadata'][:64]))

    def test_invalid_amount_has_sanitized_metadata_even_without_opt_in(self) -> None:
        '''A failed amount always identifies its fixed validation rule, never its raw content.'''
        code, result, _ = self.run_mocked(
            [DiagnosticResponse(page(amounts={10: [(PRIVATE, PRIVATE, None)]}))], metadata=False,
        )
        self.assertEqual(code, 1)
        check = result['checks']['openai_costs']
        self.assertEqual(check['failure_code'], 'invalid_usd_amount')
        self.assertEqual(check['failure_stage'], 'amount')
        self.assertEqual(check['accounting_state'], 'UNKNOWN')
        self.assertNotIn('daily_usd', check)
        self.assertEqual(check['amount_metadata']['validation_rule'], 'numeric_syntax')
        self.assertTrue(check['amount_metadata']['response_field_path'].endswith('.results[0].amount.value'))

    def test_failed_row_is_retained_separately_when_sample_budget_is_full(self) -> None:
        '''The actual rejection remains available even when earlier flagged rows filled their sample cap.'''
        records = [(RawNumber('1e-300'), f'legacy-{index}', None) for index in range(65)]
        records.append((PRIVATE, PRIVATE, None))
        code, result, _ = self.run_mocked([DiagnosticResponse(page(amounts={10: records}))])
        self.assertEqual(code, 1)
        check = result['checks']['openai_costs']
        self.assertEqual(check['observed_amount_count'], 66)
        self.assertEqual(len(check['amount_metadata']), 64)
        self.assertTrue(check['amount_metadata_truncated'])
        self.assertEqual(check['failed_amount_metadata']['validation_rule'], 'numeric_syntax')
        self.assertTrue(check['failed_amount_metadata']['response_field_path'].endswith('.results[65].amount.value'))

    def test_late_page_timeout_keeps_attempt_evidence_and_prior_shapes_without_partial_totals(self) -> None:
        '''Metadata collected before a timeout is evidence of rows, not proof of a complete cost result.'''
        first = page(tuple(range(1, 6)), amounts={1: [(RawNumber('1e-300'), PRIVATE, None)]},
                     more=True, cursor=PRIVATE)
        code, result, calls = self.run_mocked([DiagnosticResponse(first), requests.Timeout(PRIVATE)])
        self.assertEqual(code, 1)
        check = result['checks']['openai_costs']
        self.assertEqual(check['requests_attempted'], 2)
        self.assertEqual(check['http_statuses'], [200, None])
        self.assertIsNone(check['http_status'])
        self.assertEqual(check['error_category'], 'timeout')
        self.assertEqual(check['observed_amount_count'], 1)
        self.assertNotIn('daily_usd', check)
        self.assertEqual(len(calls), 2)

    def test_revalidated_exception_metadata_cannot_inject_private_fields_or_paths(self) -> None:
        '''Diagnostic formatting independently validates metadata carried by an exception.'''
        observed = []
        app.openai_decimal_value('1.25', field_path=FIELD_PATH, observer=observed.append)
        for field, invalid in (('numeric_type', PRIVATE), ('validation_rule', PRIVATE),
                               ('decimal_digit_count', PRIVATE), ('exponent', True),
                               ('response_field_path', PRIVATE)):
            mutated = {**observed[0], field: invalid}
            with self.subTest(field=field):
                self.assertIsNone(app.sanitize_amount_metadata(mutated))
                failure = smoke.diagnostic_failure(app.ReporterError(
                    PRIVATE, code='invalid_usd_amount', stage='amount',
                    details={'amount_metadata': mutated},
                ), None)
                self.assertNotIn('amount_metadata', failure)
                self.assertNotIn(PRIVATE, json.dumps(failure))
        with_extra = {**observed[0], 'raw_private_input': PRIVATE}
        sanitized = app.sanitize_amount_metadata(with_extra)
        self.assertTrue(sanitized is None or PRIVATE not in json.dumps(sanitized))


if __name__ == '__main__':
    unittest.main()
