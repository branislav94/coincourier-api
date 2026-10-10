'''Offline tests for guarded synthetic delivery and its repeatable CLI helper.'''

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

import requests

import apps_script_delivery as delivery


SERVICE_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = SERVICE_ROOT / 'scripts' / 'test_apps_script_upload.py'
HARNESS_PATH = SERVICE_ROOT / 'tests' / 'test_apps_script_receiver.cjs'
SPEC = importlib.util.spec_from_file_location('synthetic_upload_helper', HELPER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError('Synthetic upload helper could not be loaded')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

SECRET = 'a' * 64  # An intentionally synthetic fixture, never a deployment key.
URL = 'https://script.google.com/macros/s/offline-test-deployment/exec'
REPORT_DATE = date(2026, 10, 6)
NAMESPACE = 'test-offline-safety'
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    '''Use a completed UTC date regardless of the machine's current clock.'''

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        '''Return the same fixed UTC instant in the requested timezone.'''
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ResponseStub:
    '''Provide bounded HTTP bytes without contacting Apps Script or Drive.'''

    def __init__(self, body: Any) -> None:
        '''Encode one simulated receiver response and track resource cleanup.'''
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        '''Yield the mock response through the real streamed-response validator.'''
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self) -> None:
        '''Record response closure after parsing or rejection.'''
        self.closed = True


class SessionStub:
    '''Queue fake HTTP responses and retain synthetic outgoing requests.'''

    def __init__(self, responses: list[Any]) -> None:
        '''Initialize a strictly offline session with no ambient authorization.'''
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.trust_env = True

    def __enter__(self) -> SessionStub:
        '''Use the fake session in the helper's normal context manager.'''
        return self

    def __exit__(self, *args: Any) -> None:
        '''Finish the fake session without touching external resources.'''
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> ResponseStub:
        '''Serve one response or callback; never invoke a real transport.'''
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(kwargs) if callable(response) else response


def acknowledgement(duplicate: bool = False,
                    mutate: Callable[[dict[str, Any]], None] | None = None) -> Callable[..., ResponseStub]:
    '''Create a response matching the exact signed synthetic request.'''
    def response(kwargs: dict[str, Any]) -> ResponseStub:
        '''Echo only report identities and test isolation metadata, never credentials.'''
        request = json.loads(kwargs['data'])
        raw_record = base64.b64decode(request['files'][1]['content_base64'])
        namespace = json.loads(raw_record)['test_namespace']
        body = {
            'ok': True, 'version': 1, 'report_date': request['report_date'],
            'duplicate': duplicate, 'mode': 'test', 'test_namespace': namespace,
            'folder_id': 'offline_test_folder',
            'files': {
                extension: {'id': f'offline_{extension}', 'name': request['files'][index]['name'],
                            'sha256': request['files'][index]['sha256']}
                for index, extension in enumerate(('pdf', 'json'))
            },
        }
        if mutate:
            mutate(body)
        return ResponseStub(body)
    return response


class SyntheticUploadTests(unittest.TestCase):
    '''Test visibly synthetic files, protocol integrity and safe CLI behavior.'''

    def setUp(self) -> None:
        '''Create synthetic-only fixtures and deterministic timestamp checks.'''
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.pdf, self.record = helper.generate_test_artifacts(self.directory, REPORT_DATE, NAMESPACE)
        for module in (delivery, helper):
            clock = patch.object(module, 'datetime', FixedDatetime)
            clock.start()
            self.addCleanup(clock.stop)
        epoch = patch.object(delivery.time, 'time', return_value=int(NOW.timestamp()))
        epoch.start()
        self.addCleanup(epoch.stop)

    def envelope(self, **kwargs: Any) -> dict[str, Any]:
        '''Build a synthetic request through the real shared signing implementation.'''
        return delivery.build_test_envelope(REPORT_DATE, self.pdf, self.record, SECRET,
                                            NAMESPACE, **kwargs)

    def publisher(self, responses: list[Any]) -> tuple[delivery.AppsScriptPublisher, SessionStub]:
        '''Create the real publisher with mocked HTTP transport.'''
        session = SessionStub(responses)
        return delivery.AppsScriptPublisher(session, URL, SECRET), session

    def test_artifacts_are_labelled_deterministic_and_use_no_production_markers(self) -> None:
        '''Repeat generation preserves exact bytes and clearly declares synthetic provenance.'''
        self.assertEqual(self.pdf.name, 'ai-cost-report-TEST-2026-10-06.pdf')
        self.assertEqual(self.record.name, 'ai-cost-report-TEST-2026-10-06.json')
        content = self.pdf.read_bytes()
        self.assertIn(b'TEST - SYNTHETIC DELIVERY VERIFICATION', content)
        self.assertIn(b'No OpenAI or xAI billing API was called.', content)
        record = json.loads(self.record.read_bytes())
        self.assertEqual(record['data_type'], 'SYNTHETIC_DELIVERY_TEST')
        self.assertEqual(record['test_namespace'], NAMESPACE)
        self.assertTrue(all(value['synthetic'] for value in record['providers'].values()))
        other = self.directory / 'repeat'
        other.mkdir()
        repeated = helper.generate_test_artifacts(other, REPORT_DATE, NAMESPACE)
        self.assertEqual([path.read_bytes() for path in repeated], [content, self.record.read_bytes()])
        self.assertFalse(any(path.name.endswith('.delivery.json') for path in self.directory.rglob('*')))

    def test_test_signature_reuses_exact_v1_canonical_algorithm(self) -> None:
        '''TEST naming adds no alternate signing format or secret interpretation.'''
        request = self.envelope(timestamp=str(int(NOW.timestamp())), nonce='1' * 32)
        lines = ['AI-COST-REPORTER-V1', request['timestamp'], request['nonce'],
                 '2026-10-06', '0']
        for entry in request['files']:
            lines.extend((entry['name'], entry['mime_type'], str(entry['size']), entry['sha256']))
        expected = '\n'.join(lines)
        self.assertEqual(delivery.canonical_string(request), expected)
        self.assertEqual(request['signature'], hmac.new(SECRET.encode(), expected.encode(),
                                                      hashlib.sha256).hexdigest())
        self.assertFalse(request['replace'])

    def test_production_builder_still_rejects_synthetic_records(self) -> None:
        '''Synthetic provenance cannot be relabelled implicitly by the normal upload path.'''
        ordinary_pdf = self.directory / 'ai-cost-report-2026-10-06.pdf'
        ordinary_json = ordinary_pdf.with_suffix('.json')
        ordinary_pdf.write_bytes(self.pdf.read_bytes())
        ordinary_json.write_bytes(self.record.read_bytes())
        with self.assertRaises(delivery.AppsScriptDeliveryError):
            delivery.build_envelope(REPORT_DATE, ordinary_pdf, ordinary_json, SECRET)

    def test_namespace_and_test_provenance_are_required_before_transport(self) -> None:
        '''Reject traversal, invalid slugs and unlabelled reports before any request.'''
        for namespace in ('ordinary', '../test-parent', 'test-UPPER', 'test-value\n', 'test-'):
            with self.subTest(namespace=namespace):
                with self.assertRaises(delivery.AppsScriptDeliveryError):
                    delivery.build_test_envelope(REPORT_DATE, self.pdf, self.record, SECRET, namespace)
        record = json.loads(self.record.read_bytes())
        for changes in ({'data_type': 'PROVIDER_REPORTED_API_SPEND'},
                        {'test_namespace': 'test-another-namespace'}):
            altered = dict(record, **changes)
            self.record.write_text(json.dumps(altered), encoding='utf-8')
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                self.envelope()

    def test_submit_rejects_tampering_and_replacement_before_network(self) -> None:
        '''Intentional auth tests retain all file bounds, labels and integrity checks.'''
        original = self.envelope()
        mutations = [
            lambda request: request.update(replace=True),
            lambda request: request['files'][0].update(name='ai-cost-report-2026-10-06.pdf'),
            lambda request: request['files'][1].update(content_base64='AAAA'),
            lambda request: request['files'][0].update(sha256='0' * 64),
            lambda request: request.update(unexpected='value'),
        ]
        for mutation in mutations:
            request = copy.deepcopy(original)
            mutation(request)
            publisher, session = self.publisher([])
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                publisher.submit_test_envelope(request)
            self.assertEqual(session.calls, [])

    def test_test_success_requires_matching_file_and_isolation_metadata(self) -> None:
        '''HTTP 200 cannot confirm a test unless names, hashes, folder and namespace match.'''
        mutations = [
            lambda body: body.pop('mode'),
            lambda body: body.update(mode='production'),
            lambda body: body.update(test_namespace='test-wrong-destination'),
            lambda body: body.update(folder_id='../production'),
            lambda body: body.update(folder_id='offline_pdf'),
            lambda body: body.update(duplicate='true'),
            lambda body: body['files']['pdf'].update(sha256='0' * 64),
            lambda body: body['files']['json'].update(id='offline_pdf'),
            lambda body: body['files']['pdf'].update(name='ai-cost-report-2026-10-06.pdf'),
        ]
        for mutation in mutations:
            publisher, _session = self.publisher([acknowledgement(mutate=mutation)])
            with self.subTest(mutation=mutation):
                with self.assertRaises(delivery.AppsScriptDeliveryError):
                    publisher.submit_test_envelope(self.envelope())
        for malformed in (b'<html>login</html>', b'{', b'[]', b'{"ok":true,"ok":false}'):
            publisher, _session = self.publisher([ResponseStub(malformed)])
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                publisher.submit_test_envelope(self.envelope())

    def test_guard_auth_and_storage_errors_fail_safely_even_with_http_200(self) -> None:
        '''Response error codes never become success or expose untrusted remote error text.'''
        for code in ('test_uploads_disabled', 'configuration_error', 'invalid_signature',
                     'expired_timestamp', 'replayed_request', 'storage_failure'):
            publisher, _session = self.publisher([ResponseStub({'ok': False, 'error': code})])
            with self.assertRaisesRegex(delivery.AppsScriptDeliveryError, code):
                publisher.submit_test_envelope(self.envelope())
        publisher, _session = self.publisher([ResponseStub({'ok': False, 'error': SECRET})])
        with self.assertRaises(delivery.AppsScriptDeliveryError) as caught:
            publisher.submit_test_envelope(self.envelope())
        self.assertNotIn(SECRET, str(caught.exception))

    def test_rejection_checks_reach_receiver_without_locally_repairing_authentication(self) -> None:
        '''An invalid HMAC and expired timestamp are sent intact for remote rejection checks.'''
        publisher, session = self.publisher([
            ResponseStub({'ok': False, 'error': 'invalid_signature'}),
            ResponseStub({'ok': False, 'error': 'expired_timestamp'}),
        ])
        invalid = self.envelope()
        invalid['signature'] = '0' * 64
        expired = self.envelope(timestamp=str(int(NOW.timestamp()) - 301))
        for request in (invalid, expired):
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                publisher.submit_test_envelope(request)
        self.assertEqual(json.loads(session.calls[0][2]['data'])['signature'], '0' * 64)
        self.assertEqual(json.loads(session.calls[1][2]['data'])['timestamp'], expired['timestamp'])
        self.assertFalse(session.trust_env)

    def test_helper_checks_three_successes_duplicate_identity_and_hmac_rejection(self) -> None:
        '''Repeatable verification demonstrates request behavior without claiming Drive inspection.'''
        publisher, session = self.publisher([
            acknowledgement(), acknowledgement(duplicate=True),
            ResponseStub({'ok': False, 'error': 'invalid_signature'}),
            acknowledgement(duplicate=True),
        ])
        summary = helper.run_live_test(publisher, REPORT_DATE, self.pdf, self.record, NAMESPACE)
        self.assertEqual(summary['status'], 'api_confirmed')
        self.assertEqual(summary['successful_requests'], 3)
        self.assertEqual(summary['rejected_requests'], {'invalid_signature': 1})
        self.assertIn('not performed', summary['independent_drive_inspection'])
        posted = [json.loads(call[2]['data']) for call in session.calls]
        self.assertEqual(len({request['nonce'] for request in posted}), 4)
        self.assertTrue(all(request['files'] == posted[0]['files'] for request in posted))
        self.assertTrue(all(not request['replace'] for request in posted))
        for index, request in enumerate(posted):
            matches = request['signature'] == delivery.sign_envelope(request, SECRET)
            self.assertEqual(matches, index != 2)

    def test_helper_fails_if_retry_is_not_duplicate_or_changes_identity(self) -> None:
        '''Reject apparent successful reruns that do not prove stable test file identities.'''
        second_responses = [
            acknowledgement(duplicate=False),
            acknowledgement(duplicate=True, mutate=lambda body: body['files']['pdf'].update(id='new_pdf')),
            acknowledgement(duplicate=True, mutate=lambda body: body.update(folder_id='new_folder')),
        ]
        for second in second_responses:
            publisher, session = self.publisher([acknowledgement(), second])
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                helper.run_live_test(publisher, REPORT_DATE, self.pdf, self.record, NAMESPACE)
            self.assertEqual(len(session.calls), 2)

    def test_helper_requires_exact_invalid_signature_rejection_and_stable_final_retry(self) -> None:
        '''A generic failure, invalid-signature acceptance or changed final ID cannot pass.'''
        wrong_rejections = [
            ResponseStub({'ok': False, 'error': 'storage_failure'}), acknowledgement(duplicate=True),
        ]
        for response in wrong_rejections:
            publisher, session = self.publisher([acknowledgement(), acknowledgement(True), response])
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                helper.run_live_test(publisher, REPORT_DATE, self.pdf, self.record, NAMESPACE)
            self.assertEqual(len(session.calls), 3)
        publisher, _session = self.publisher([
            acknowledgement(), acknowledgement(True),
            ResponseStub({'ok': False, 'error': 'invalid_signature'}),
            acknowledgement(True, mutate=lambda body: body['files']['json'].update(id='changed_json')),
        ])
        with self.assertRaises(delivery.AppsScriptDeliveryError):
            helper.run_live_test(publisher, REPORT_DATE, self.pdf, self.record, NAMESPACE)

    def test_default_cli_is_offline_without_even_reading_environment_secret(self) -> None:
        '''Preparation must not touch configured credentials or create an HTTP session.'''
        output = io.StringIO()
        original_get = os.environ.get

        def guarded_get(name: str, default: Any = None) -> Any:
            '''Fail if offline preparation attempts to read the protected shared secret.'''
            if name == 'GOOGLE_APPS_SCRIPT_SHARED_SECRET':
                raise AssertionError('Offline helper must not read a configured secret')
            return original_get(name, default)

        with patch.dict(os.environ, {'GOOGLE_APPS_SCRIPT_SHARED_SECRET': SECRET}, clear=True), \
             patch.object(os.environ, 'get', side_effect=guarded_get), \
             patch.object(helper.requests, 'Session') as session, \
             redirect_stdout(output):
            status = helper.main(['--url', URL, '--namespace', NAMESPACE, '--date', '2026-10-06'])
            session.assert_not_called()
        self.assertEqual(status, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['status'], 'prepared_offline')
        self.assertFalse(summary['network_requests'])
        self.assertTrue(all('-TEST-' in file['name'] for file in summary['files']))
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn('signature', output.getvalue())

    def test_live_cli_missing_secret_or_bad_endpoint_fails_before_requests(self) -> None:
        '''Opting into live mode still fails safely on incomplete or unsafe configuration.'''
        for arguments in (['--live', '--url', URL],
                          ['--live', '--url', 'https://attacker.invalid/upload']):
            errors = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(helper.requests, 'Session') as session, \
                 patch.object(helper, 'generate_test_artifacts') as artifacts, \
                 redirect_stderr(errors):
                status = helper.main(arguments)
                session.assert_not_called()
                artifacts.assert_not_called()
            self.assertEqual(status, 1)
            self.assertIn('TEST FAILED:', errors.getvalue())

    def test_live_cli_malformed_secret_is_not_logged_and_never_posts(self) -> None:
        '''Configuration validation cannot echo the supplied key or start delivery.'''
        malformed = 'synthetic-malformed-sensitive-value'
        errors = io.StringIO()
        with patch.dict(os.environ, {'GOOGLE_APPS_SCRIPT_SHARED_SECRET': malformed}, clear=True), \
             patch.object(helper.requests, 'Session') as session, redirect_stderr(errors):
            status = helper.main(['--live', '--url', URL, '--namespace', NAMESPACE,
                                  '--date', '2026-10-06'])
            session.assert_not_called()
        self.assertEqual(status, 1)
        self.assertNotIn(malformed, errors.getvalue())

    def test_live_cli_can_confirm_mocked_requests_without_claiming_drive_inspection(self) -> None:
        '''The opt-in CLI uses the real protocol over fake HTTP and emits only safe evidence.'''
        session = SessionStub([
            acknowledgement(), acknowledgement(True),
            ResponseStub({'ok': False, 'error': 'invalid_signature'}), acknowledgement(True),
        ])
        output = io.StringIO()
        with patch.dict(os.environ, {'GOOGLE_APPS_SCRIPT_SHARED_SECRET': SECRET}, clear=True), \
             patch.object(helper.requests, 'Session', return_value=session), redirect_stdout(output):
            status = helper.main(['--live', '--url', URL, '--namespace', NAMESPACE,
                                  '--date', '2026-10-06'])
        self.assertEqual(status, 0)
        self.assertEqual(len(session.calls), 4)
        self.assertFalse(session.trust_env)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['status'], 'api_confirmed')
        self.assertTrue(summary['network_requests'])
        self.assertIn('not performed', summary['independent_drive_inspection'])
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn('signature', json.dumps(summary['files']))

    @unittest.skipUnless(shutil.which('node'), 'Node is required for actual receiver verification')
    def test_python_signed_test_request_obeys_actual_receiver_gate_and_isolation(self) -> None:
        '''Run Python TEST envelopes through real Code.gs with in-memory Google services.'''
        request = self.envelope(timestamp=str(int(NOW.timestamp())), nonce='1' * 32)
        for enabled in (False, True):
            result = subprocess.run(
                ['node', str(HARNESS_PATH), '--request'],
                input=json.dumps({'envelope': request, 'now': int(NOW.timestamp()),
                                 'enable_test_uploads': enabled}),
                text=True, capture_output=True, timeout=20, check=True,
            )
            body = json.loads(result.stdout)
            self.assertEqual(body['response']['ok'], enabled)
            self.assertEqual(body['file_count'], 2 if enabled else 0)
            if enabled:
                self.assertEqual(body['response']['mode'], 'test')
                self.assertEqual(body['response']['test_namespace'], NAMESPACE)
            else:
                self.assertEqual(body['drive_operations'], 0)


if __name__ == '__main__':
    unittest.main()
