'''Offline protocol, transport and actual Apps Script receiver regression tests.'''

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

import requests

import app
import apps_script_delivery as delivery


SECRET = 'a' * 64  # Synthetic offline fixture; never a deployment credential.
WEB_APP_URL = 'https://script.google.com/macros/s/offline-test-deployment/exec'
REPORT_DATE = date(2026, 10, 6)
FIXED_NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
NOW_SECONDS = int(FIXED_NOW.timestamp())
HARNESS = Path(__file__).with_name('test_apps_script_receiver.cjs')


class FixedDatetime(datetime):
    '''Keep completed-day checks reproducible regardless of the host clock.'''

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        '''Return the fixed current UTC instant in the requested timezone.'''
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


class ResponseStub:
    '''Stream a bounded mocked HTTP response without contacting Google.'''

    def __init__(self, body: Any = None, status_code: int = 200,
                 headers: dict[str, str] | None = None,
                 stream_error: Exception | None = None) -> None:
        '''Store raw bytes or serialize the provided JSON-compatible body.'''
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.status_code = status_code
        self.headers = headers or {}
        self.stream_error = stream_error
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        '''Yield response chunks, or simulate a connection failure midstream.'''
        if self.stream_error:
            raise self.stream_error
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self) -> None:
        '''Record that transport resources were released.'''
        self.closed = True


class SessionStub:
    '''Capture requests and queue responses, exceptions or response callbacks.'''

    def __init__(self, responses: list[Any]) -> None:
        '''Initialize a strictly offline response queue.'''
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> ResponseStub:
        '''Record the outgoing request and serve the next mocked response.'''
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(method, url, kwargs) if callable(response) else response


def success_body(envelope: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
    '''Build the documented receiver acknowledgement for a synthetic request.'''
    return {
        'ok': True, 'version': 1, 'report_date': envelope['report_date'],
        'duplicate': duplicate,
        'files': {
            extension: {'id': f'{extension}_offline_id', 'name': envelope['files'][index]['name'],
                        'sha256': envelope['files'][index]['sha256']}
            for index, extension in enumerate(('pdf', 'json'))
        },
    }


def acknowledge(duplicate: bool = False,
                mutate: Callable[[dict[str, Any]], None] | None = None) -> Callable[..., ResponseStub]:
    '''Create a response callback that acknowledges exactly the posted artifacts.'''
    def response(method: str, url: str, kwargs: dict[str, Any]) -> ResponseStub:
        '''Read the synthetic request and respond with matching identities/hashes.'''
        envelope = json.loads(kwargs['data'])
        result = success_body(envelope, duplicate)
        if mutate:
            mutate(result)
        return ResponseStub(result)
    return response


class AppsScriptDeliveryTests(unittest.TestCase):
    '''Verify bounded signing, safe HTTP handling and response-body validation.'''

    def setUp(self) -> None:
        '''Create private temporary report fixtures and a deterministic UTC clock.'''
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.pdf = self.directory / f'ai-cost-report-{REPORT_DATE.isoformat()}.pdf'
        self.metadata = self.pdf.with_suffix('.json')
        self.pdf.write_bytes(b'%PDF-1.4\nsynthetic offline fixture\n%%EOF\n')
        self.report = app.BillingReport(
            report_date=REPORT_DATE,
            openai=app.ProviderSpend(Decimal('1.20'), Decimal('7.40')),
            xai=app.ProviderSpend(Decimal('2.30'), Decimal('8.50')),
            generated_at=FIXED_NOW,
        )
        self.metadata.write_text(json.dumps(app.report_to_dict(self.report)), encoding='utf-8')
        self.addCleanup(patch.stopall)
        patch.object(delivery, 'datetime', FixedDatetime).start()
        patch.object(delivery.time, 'time', return_value=NOW_SECONDS).start()

    def build(self, **kwargs: Any) -> dict[str, Any]:
        '''Build a deterministic signed envelope from the temporary fixtures.'''
        return delivery.build_envelope(REPORT_DATE, self.pdf, self.metadata, SECRET,
                                       nonce='1' * 32, timestamp=str(NOW_SECONDS), **kwargs)

    def publish(self, session: SessionStub, replace: bool = False) -> dict[str, str]:
        '''Publish through the real implementation with a strictly mocked session.'''
        return delivery.AppsScriptPublisher(session, WEB_APP_URL, SECRET).publish(
            REPORT_DATE, self.pdf, self.metadata, replace=replace
        )

    def test_exact_canonical_algorithm_and_literal_utf8_secret(self) -> None:
        '''Compare the canonical LF format and HMAC to independent standard-library code.'''
        envelope = self.build()
        pdf_hash = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        json_hash = hashlib.sha256(self.metadata.read_bytes()).hexdigest()
        expected = '\n'.join([
            'AI-COST-REPORTER-V1', str(NOW_SECONDS), '1' * 32, '2026-10-06', '0',
            self.pdf.name, 'application/pdf', str(self.pdf.stat().st_size), pdf_hash,
            self.metadata.name, 'application/json', str(self.metadata.stat().st_size), json_hash,
        ])
        self.assertEqual(delivery.canonical_string(envelope), expected)
        self.assertFalse(expected.endswith('\n'))
        signature = hmac.new(SECRET.encode('utf-8'), expected.encode('utf-8'),
                             hashlib.sha256).hexdigest()
        self.assertEqual(envelope['signature'], signature)
        decoded_key_signature = hmac.new(bytes.fromhex(SECRET), expected.encode('utf-8'),
                                         hashlib.sha256).hexdigest()
        self.assertNotEqual(signature, decoded_key_signature)
        for record, artifact in zip(envelope['files'], (self.pdf, self.metadata)):
            self.assertEqual(base64.b64decode(record['content_base64']), artifact.read_bytes())

    def test_successful_upload_posts_signature_in_body_and_validates_hashes(self) -> None:
        '''A successful HTTP response must match both artifact identities and hashes.'''
        session = SessionStub([acknowledge()])
        self.assertEqual(self.publish(session), {'pdf': 'pdf_offline_id', 'json': 'json_offline_id'})
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ('POST', WEB_APP_URL))
        self.assertEqual(kwargs['headers'], {'Content-Type': 'application/json'})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertTrue(kwargs['verify'])
        self.assertTrue(kwargs['stream'])
        envelope = json.loads(kwargs['data'])
        self.assertEqual(envelope['signature'], delivery.sign_envelope(envelope, SECRET))
        self.assertNotIn(SECRET, kwargs['data'].decode('utf-8'))

    def test_duplicate_retry_uses_fresh_nonce_and_preserves_report_bytes(self) -> None:
        '''A lost local acknowledgement can safely retry the same report with a new nonce.'''
        original = (self.pdf.read_bytes(), self.metadata.read_bytes())
        session = SessionStub([acknowledge(), acknowledge(duplicate=True)])
        first = self.publish(session)
        second = self.publish(session)
        self.assertEqual(first, second)
        requests_sent = [json.loads(call[2]['data']) for call in session.calls]
        self.assertNotEqual(requests_sent[0]['nonce'], requests_sent[1]['nonce'])
        self.assertEqual(requests_sent[0]['files'], requests_sent[1]['files'])
        self.assertEqual(original, (self.pdf.read_bytes(), self.metadata.read_bytes()))

    def test_replace_flag_is_authenticated(self) -> None:
        '''Explicit reconciliation is included in the canonical signing string.'''
        session = SessionStub([acknowledge()])
        self.publish(session, replace=True)
        envelope = json.loads(session.calls[0][2]['data'])
        self.assertTrue(envelope['replace'])
        signature = envelope['signature']
        envelope['replace'] = False
        self.assertNotEqual(signature, delivery.sign_envelope(envelope, SECRET))

    def test_invalid_signature_expiry_replay_and_delivery_fail_even_on_http_200(self) -> None:
        '''Apps Script error objects must never be mistaken for delivery success.'''
        for error in ('invalid_signature', 'expired_timestamp', 'replayed_request',
                      'storage_failure', 'busy', 'conflict'):
            with self.subTest(error=error):
                with self.assertRaises(delivery.AppsScriptDeliveryError):
                    self.publish(SessionStub([ResponseStub({'ok': False, 'error': error})]))

    def test_unknown_error_response_does_not_echo_remote_contents(self) -> None:
        '''Untrusted error text cannot disclose request contents or credentials in logs.'''
        body = {'ok': False, 'error': f'remote echoed synthetic secret {SECRET}'}
        with self.assertRaises(delivery.AppsScriptDeliveryError) as caught:
            self.publish(SessionStub([ResponseStub(body)]))
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn('remote echoed', str(caught.exception))

    def test_malformed_http_200_responses_are_rejected(self) -> None:
        '''Reject HTML, ambiguous JSON, wrong types and mismatched success identities.'''
        for body in (b'<html>Sign in</html>', b'{', [], None,
                     b'{"ok":true,"ok":false}', b'{"ok":NaN}', b'\xff'):
            with self.subTest(body_type=type(body).__name__):
                with self.assertRaises(delivery.AppsScriptDeliveryError):
                    self.publish(SessionStub([ResponseStub(body)]))
        mutations = [
            lambda body: body.update(ok='true'),
            lambda body: body.update(version=True),
            lambda body: body.update(report_date='2026-10-05'),
            lambda body: body.update(duplicate=0),
            lambda body: body['files'].pop('json'),
            lambda body: body['files']['pdf'].update(id=''),
            lambda body: body['files']['pdf'].update(id='../other'),
            lambda body: body['files']['json'].update(id=body['files']['pdf']['id']),
            lambda body: body['files']['json'].update(name='other.json'),
            lambda body: body['files']['pdf'].update(sha256='0' * 64),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(delivery.AppsScriptDeliveryError):
                    self.publish(SessionStub([acknowledge(mutate=mutation)]))

    def test_network_http_and_stream_failures_are_safe(self) -> None:
        '''Request and streamed-response failures surface without secrets or remote bodies.'''
        responses = [
            requests.ConnectionError(f'synthetic exception {SECRET}'),
            ResponseStub({'private': SECRET}, status_code=503),
            ResponseStub(stream_error=requests.ConnectionError(SECRET)),
        ]
        for response in responses:
            with self.subTest(response_type=type(response).__name__):
                with self.assertRaises(delivery.AppsScriptDeliveryError) as caught:
                    self.publish(SessionStub([response]))
                self.assertNotIn(SECRET, str(caught.exception))
                if isinstance(response, ResponseStub):
                    self.assertTrue(response.closed)

    def test_content_service_redirect_retrieves_response_without_resending_files(self) -> None:
        '''Follow the documented response redirect using GET with no signature or body.'''
        redirect = ResponseStub(status_code=302, headers={
            'Location': 'https://script.googleusercontent.com/macros/echo?opaque=offline',
        })
        body = success_body(self.build())
        final = ResponseStub(body)
        session = SessionStub([redirect, final])
        self.assertEqual(self.publish(session)['pdf'], 'pdf_offline_id')
        self.assertEqual([call[0] for call in session.calls], ['POST', 'GET'])
        self.assertNotIn('data', session.calls[1][2])
        self.assertNotIn('headers', session.calls[1][2])
        self.assertTrue(redirect.closed)
        self.assertTrue(final.closed)

    def test_unsafe_redirects_and_second_redirect_are_rejected(self) -> None:
        '''Reject external/login hosts, HTTPS downgrade and any body-preserving redirect.'''
        destinations = [
            'https://example.invalid/report', 'http://script.googleusercontent.com/echo',
            'https://script.googleusercontent.com.attacker.invalid/echo',
            'https://script.googleusercontent.com@attacker.invalid/echo',
            'https://accounts.google.com/signin',
            'https://script.googleusercontent.com/echo#fragment',
        ]
        for destination in destinations:
            session = SessionStub([ResponseStub(status_code=302, headers={'Location': destination})])
            with self.subTest(destination=destination):
                with self.assertRaises(delivery.AppsScriptDeliveryError):
                    self.publish(session)
                self.assertEqual(len(session.calls), 1)
        for status in (307, 308):
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                self.publish(SessionStub([ResponseStub(status_code=status)]))
        session = SessionStub([
            ResponseStub(status_code=303, headers={
                'Location': 'https://script.googleusercontent.com/macros/echo?offline=1',
            }), ResponseStub(status_code=302),
        ])
        with self.assertRaises(delivery.AppsScriptDeliveryError):
            self.publish(session)
        self.assertEqual(len(session.calls), 2)

    def test_response_size_is_bounded_even_without_content_length(self) -> None:
        '''Enforce declared and actual response limits before JSON parsing.'''
        for response in (
            ResponseStub(headers={'Content-Length': str(delivery.MAX_RESPONSE_BYTES + 1)}),
            ResponseStub(body=b' ' * (delivery.MAX_RESPONSE_BYTES + 1)),
            ResponseStub(headers={'Content-Length': '-1'}),
        ):
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                self.publish(SessionStub([response]))
            self.assertTrue(response.closed)

    def test_configuration_rejects_bad_secret_or_endpoint_without_network(self) -> None:
        '''Only a deployed HTTPS web app URL and the exact shared-secret format are accepted.'''
        session = SessionStub([])
        for secret in ('', 'short', 'A' * 64, 'a' * 63, 'a' * 64 + '\n'):
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                delivery.AppsScriptPublisher(session, WEB_APP_URL, secret)
        for url in ('http://script.google.com/macros/s/test/exec',
                    'https://example.invalid/macros/s/test/exec',
                    'https://script.google.com/macros/s/test/dev',
                    WEB_APP_URL + '?secret=hidden', WEB_APP_URL + '#fragment'):
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                delivery.AppsScriptPublisher(session, url, SECRET)
        self.assertEqual(session.calls, [])

    def test_artifact_name_bounds_format_and_provenance_are_checked(self) -> None:
        '''Reject invalid files locally before transmitting any payload.'''
        original_pdf = self.pdf.read_bytes()
        for content in (b'', b'not a PDF', b'%PDF-1.4 no end marker',
                        b'%PDF-' + b'x' * delivery.MAX_PDF_BYTES + b'%%EOF'):
            self.pdf.write_bytes(content)
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                self.build()
        self.pdf.write_bytes(original_pdf)
        wrong_name = self.directory / 'unrelated.pdf'
        wrong_name.write_bytes(original_pdf)
        with self.assertRaises(delivery.AppsScriptDeliveryError):
            delivery.build_envelope(REPORT_DATE, wrong_name, self.metadata, SECRET)
        original_json = self.metadata.read_bytes()
        for content in (b'', b'{}', b'{"ok":true,"ok":false}', b'{"value":NaN}',
                        b'\xff', b' ' * (delivery.MAX_JSON_BYTES + 1)):
            self.metadata.write_bytes(content)
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                self.build()
        for changed in ({'data_type': 'SYNTHETIC_DEMO'}, {'currency': 'EUR'},
                        {'report_date_utc': '2026-10-05'}, {'providers': {'openai': {}}}):
            record = json.loads(original_json)
            record.update(changed)
            self.metadata.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                self.build()

    def test_invalid_day_and_signing_fields_are_rejected(self) -> None:
        '''Ensure dates are complete UTC days and signed scalar values have one encoding.'''
        for invalid_date in (date(1999, 12, 31), FIXED_NOW.date(), date(2026, 10, 8)):
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                delivery.build_envelope(invalid_date, self.pdf, self.metadata, SECRET)
        for kwargs in ({'timestamp': '01'}, {'timestamp': '+1'}, {'timestamp': '1.0'},
                       {'nonce': 'bad'}, {'nonce': 'A' * 32}, {'replace': 'false'}):
            with self.assertRaises(delivery.AppsScriptDeliveryError):
                delivery.build_envelope(REPORT_DATE, self.pdf, self.metadata, SECRET, **kwargs)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the actual receiver harness')
    def test_python_payload_is_accepted_by_actual_apps_script_and_mutations_fail(self) -> None:
        '''Run real Python signatures through Code.gs with mocked Google services.'''
        good = self.build()
        invalid = dict(good, signature='0' * 64)
        expired = dict(good, timestamp=str(NOW_SECONDS - 301))
        expired['signature'] = delivery.sign_envelope(expired, SECRET)
        for request, accepted in ((good, True), (invalid, False), (expired, False)):
            with self.subTest(accepted=accepted):
                result = subprocess.run(
                    ['node', str(HARNESS), '--request'],
                    input=json.dumps({'envelope': request, 'now': NOW_SECONDS}),
                    text=True, capture_output=True, check=True, timeout=20,
                )
                output = json.loads(result.stdout)
                self.assertEqual(output['response']['ok'], accepted)
                self.assertEqual(output['file_count'], 2 if accepted else 0)
                if not accepted:
                    self.assertEqual(output['drive_operations'], 0)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the actual receiver harness')
    def test_actual_apps_script_receiver_security_and_retry_suite(self) -> None:
        '''Exercise real receiver code through the independent local JavaScript harness.'''
        result = subprocess.run(['node', str(HARNESS)], text=True, capture_output=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Apps Script receiver tests passed.', result.stdout)


class AppsScriptApplicationTests(unittest.TestCase):
    '''Verify application delivery markers, snapshot reuse and OAuth independence.'''

    def setUp(self) -> None:
        '''Set only synthetic Apps Script configuration and deterministic provider results.'''
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.report = app.BillingReport(
            report_date=REPORT_DATE,
            openai=app.ProviderSpend(Decimal('1.20'), Decimal('7.40')),
            xai=app.ProviderSpend(Decimal('2.30'), Decimal('8.50')),
            generated_at=FIXED_NOW,
        )
        environment = {
            'GOOGLE_DRIVE_ENABLED': 'true', 'GOOGLE_DRIVE_BACKEND': 'apps_script',
            'GOOGLE_APPS_SCRIPT_WEB_APP_URL': WEB_APP_URL,
            'GOOGLE_APPS_SCRIPT_SHARED_SECRET': SECRET,
            'EMAIL_TO': '', 'WHATSAPP_TO': '',
        }
        self.addCleanup(patch.stopall)
        patch.dict('os.environ', environment, clear=True).start()
        patch.object(app, 'datetime', FixedDatetime).start()

    def test_apps_script_backend_needs_no_oauth_and_records_success_once(self) -> None:
        '''Successful delivery records both remote IDs and skips provider/delivery reruns.'''
        with patch('app.get_live_report', return_value=self.report) as billing, \
             patch('app.refresh_access_token') as oauth, \
             patch('app.AppsScriptPublisher') as publisher:
            publisher.return_value.publish.return_value = {'pdf': 'offline_pdf', 'json': 'offline_json'}
            for _ in range(2):
                app.run_report(REPORT_DATE, self.directory, False, False, False)
            billing.assert_called_once()
            publisher.return_value.publish.assert_called_once()
            oauth.assert_not_called()
        marker = json.loads((self.directory / 'ai-cost-report-2026-10-06.delivery.json')
                            .read_text(encoding='utf-8'))
        self.assertTrue(marker['google_drive'])
        self.assertEqual(marker['google_drive_file_ids'], {'pdf': 'offline_pdf', 'json': 'offline_json'})

    def test_delivery_failure_retains_snapshot_and_retries_without_provider_refetch(self) -> None:
        '''A partial failure cannot mark success or change the billing snapshot on retry.'''
        with patch('app.get_live_report', return_value=self.report) as billing, \
             patch('app.AppsScriptPublisher') as publisher:
            publisher.return_value.publish.side_effect = [
                delivery.AppsScriptDeliveryError('offline delivery failure'),
                {'pdf': 'offline_pdf', 'json': 'offline_json'},
            ]
            with self.assertRaisesRegex(app.ReporterError, 'offline delivery failure'):
                app.run_report(REPORT_DATE, self.directory, False, False, False)
            pdf = self.directory / 'ai-cost-report-2026-10-06.pdf'
            snapshot = pdf.read_bytes()
            marker = json.loads(pdf.with_suffix('.delivery.json').read_text(encoding='utf-8'))
            self.assertFalse(marker['google_drive'])
            app.run_report(REPORT_DATE, self.directory, False, False, False)
            self.assertEqual(pdf.read_bytes(), snapshot)
            billing.assert_called_once()
            self.assertEqual(publisher.return_value.publish.call_count, 2)

    def test_bad_apps_script_configuration_fails_before_provider_api_calls(self) -> None:
        '''Missing required configuration fails before requesting actual billing data.'''
        with patch.dict('os.environ', {'GOOGLE_APPS_SCRIPT_SHARED_SECRET': ''}), \
             patch('app.get_live_report') as billing:
            with self.assertRaises(app.ReporterError):
                app.run_report(REPORT_DATE, self.directory, False, False, False)
            billing.assert_not_called()

    def test_force_resend_authenticates_replacement(self) -> None:
        '''Explicit force resend reaches the new backend as a replacement request.'''
        with patch('app.get_live_report', return_value=self.report), \
             patch('app.AppsScriptPublisher') as publisher:
            publisher.return_value.publish.return_value = {'pdf': 'offline_pdf', 'json': 'offline_json'}
            app.run_report(REPORT_DATE, self.directory, False, False, True)
            self.assertTrue(publisher.return_value.publish.call_args.kwargs.get('replace'))

    def test_force_resend_intent_survives_a_partial_failure_and_plain_retry(self) -> None:
        '''A plain retry after reconciliation failure retains the authenticated replace intent.'''
        with patch('app.get_live_report', return_value=self.report) as billing, \
             patch('app.AppsScriptPublisher') as publisher:
            publisher.return_value.publish.side_effect = [
                delivery.AppsScriptDeliveryError('offline replacement failed'),
                {'pdf': 'offline_pdf', 'json': 'offline_json'},
            ]
            with self.assertRaises(app.ReporterError):
                app.run_report(REPORT_DATE, self.directory, False, False, True)
            marker_path = self.directory / 'ai-cost-report-2026-10-06.delivery.json'
            marker = json.loads(marker_path.read_text(encoding='utf-8'))
            self.assertTrue(marker['google_drive_replace'])
            pdf = marker_path.with_name('ai-cost-report-2026-10-06.pdf')
            snapshot = pdf.read_bytes()
            app.run_report(REPORT_DATE, self.directory, False, False, False)
            self.assertEqual(pdf.read_bytes(), snapshot)
            billing.assert_called_once()
            self.assertTrue(all(call.kwargs['replace']
                                for call in publisher.return_value.publish.call_args_list))
            marker = json.loads(marker_path.read_text(encoding='utf-8'))
            self.assertFalse(marker['google_drive_replace'])

    def test_changed_destination_redelivers_original_snapshot(self) -> None:
        '''A marker for another web app cannot skip delivery to a newly configured destination.'''
        with patch('app.get_live_report', return_value=self.report) as billing, \
             patch('app.AppsScriptPublisher') as publisher:
            publisher.return_value.publish.return_value = {'pdf': 'offline_pdf', 'json': 'offline_json'}
            app.run_report(REPORT_DATE, self.directory, False, False, False)
            pdf = self.directory / 'ai-cost-report-2026-10-06.pdf'
            snapshot = pdf.read_bytes()
            alternate = 'https://script.google.com/macros/s/another-offline-deployment/exec'
            with patch.dict('os.environ', {'GOOGLE_APPS_SCRIPT_WEB_APP_URL': alternate}):
                app.run_report(REPORT_DATE, self.directory, False, False, False)
            self.assertEqual(pdf.read_bytes(), snapshot)
            billing.assert_called_once()
            self.assertEqual(publisher.return_value.publish.call_count, 2)
            marker = json.loads(pdf.with_suffix('.delivery.json').read_text(encoding='utf-8'))
            self.assertEqual(marker['google_drive_target'], hashlib.sha256(alternate.encode()).hexdigest())
            self.assertNotIn(alternate, json.dumps(marker))

    def test_changed_destination_does_not_inherit_pending_replacement(self) -> None:
        '''A failed reconciliation grants replacement only for its original destination.'''
        with patch('app.get_live_report', return_value=self.report) as billing, \
             patch('app.AppsScriptPublisher') as publisher:
            publisher.return_value.publish.side_effect = [
                delivery.AppsScriptDeliveryError('offline replacement failed'),
                {'pdf': 'offline_pdf', 'json': 'offline_json'},
            ]
            with self.assertRaises(app.ReporterError):
                app.run_report(REPORT_DATE, self.directory, False, False, True)
            pdf = self.directory / 'ai-cost-report-2026-10-06.pdf'
            snapshot = pdf.read_bytes()
            alternate = 'https://script.google.com/macros/s/another-offline-deployment/exec'
            with patch.dict('os.environ', {'GOOGLE_APPS_SCRIPT_WEB_APP_URL': alternate}):
                app.run_report(REPORT_DATE, self.directory, False, False, False)
            self.assertEqual(pdf.read_bytes(), snapshot)
            billing.assert_called_once()
            calls = publisher.return_value.publish.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0].kwargs['replace'])
            self.assertFalse(calls[1].kwargs['replace'])

    def test_no_send_protects_existing_snapshot_even_with_force_resend(self) -> None:
        '''Preview runs cannot overwrite a completed or partially delivered snapshot.'''
        pdf = self.directory / 'ai-cost-report-2026-10-06.pdf'
        app.create_pdf(self.report, pdf)
        metadata = pdf.with_suffix('.json')
        marker_path = pdf.with_suffix('.delivery.json')
        app.write_json_atomic(metadata, app.report_to_dict(self.report))
        for delivered in (False, True):
            app.write_json_atomic(marker_path, {
                'email': False, 'whatsapp': [], 'google_drive': delivered,
                'google_drive_backend': 'apps_script',
                'google_drive_target': hashlib.sha256(WEB_APP_URL.encode()).hexdigest(),
                'google_drive_replace': not delivered,
            })
            snapshot = (pdf.read_bytes(), metadata.read_bytes(), marker_path.read_bytes())
            for force_resend in (False, True):
                with self.subTest(delivered=delivered, force_resend=force_resend), \
                     patch('app.get_live_report') as billing, \
                     patch('app.AppsScriptPublisher') as publisher, \
                     patch('app.refresh_access_token') as oauth:
                    with self.assertRaisesRegex(app.ReporterError, 'snapshot is protected'):
                        app.run_report(REPORT_DATE, self.directory, False, True, force_resend)
                    billing.assert_not_called()
                    publisher.assert_not_called()
                    oauth.assert_not_called()
                    self.assertEqual(snapshot, (pdf.read_bytes(), metadata.read_bytes(),
                                                marker_path.read_bytes()))

    def test_apps_script_date_before_2000_fails_before_billing(self) -> None:
        '''Receiver date bounds must fail locally before either provider is contacted.'''
        with patch('app.get_live_report') as billing, \
             patch('app.AppsScriptPublisher') as publisher:
            with self.assertRaisesRegex(app.ReporterError, '2000 onward'):
                app.run_report(date(1999, 12, 31), self.directory, False, False, False)
            billing.assert_not_called()
            publisher.return_value.publish.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_legacy_oauth_marker_does_not_skip_apps_script_delivery(self) -> None:
        '''An old OAuth success marker cannot silently satisfy a new Apps Script destination.'''
        pdf = self.directory / 'ai-cost-report-2026-10-06.pdf'
        app.create_pdf(self.report, pdf)
        app.write_json_atomic(pdf.with_suffix('.json'), app.report_to_dict(self.report))
        app.write_json_atomic(pdf.with_suffix('.delivery.json'), {
            'email': False, 'whatsapp': [], 'google_drive': True,
            'google_drive_file_ids': {'pdf': 'legacy_pdf', 'json': 'legacy_json'},
        })
        with patch('app.get_live_report') as billing, \
             patch('app.refresh_access_token') as oauth, \
             patch('app.AppsScriptPublisher') as publisher:
            publisher.return_value.publish.return_value = {'pdf': 'offline_pdf', 'json': 'offline_json'}
            app.run_report(REPORT_DATE, self.directory, False, False, False)
            billing.assert_not_called()
            oauth.assert_not_called()
            publisher.return_value.publish.assert_called_once()


if __name__ == '__main__':
    unittest.main()
