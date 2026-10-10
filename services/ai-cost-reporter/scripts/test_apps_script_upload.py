'''Verify isolated Apps Script delivery using synthetic artifacts and no billing APIs.'''

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps_script_delivery import (  # noqa: E402
    AppsScriptDeliveryError,
    AppsScriptPublisher,
    build_test_envelope,
    validate_web_app_url,
)


DEFAULT_WEB_APP_URL = (
    'https://script.google.com/macros/s/'
    'AKfycbzfldKnu2CiZ4pYNDPyjF-NqxTsu5fMNpvuhCt3SBU-rUrfOqkCy99emCavg7KAORH1/exec'
)


def generate_test_artifacts(
    directory: Path, report_date: date, test_namespace: str,
) -> tuple[Path, Path]:
    '''Write deterministic, visibly synthetic files into a temporary test directory.

    The same date and namespace produce identical bytes on later runs. These
    files never use production report names, storage or delivery markers.
    '''
    stem = f'ai-cost-report-TEST-{report_date.isoformat()}'
    pdf_path = directory / f'{stem}.pdf'
    json_path = directory / f'{stem}.json'
    document = canvas.Canvas(str(pdf_path), invariant=1, pageCompression=0)
    document.setTitle('TEST - AI Cost Reporter delivery verification')
    document.setAuthor('AI Cost Reporter synthetic integration test')
    document.setFont('Helvetica-Bold', 18)
    document.drawString(45, 780, 'TEST - SYNTHETIC DELIVERY VERIFICATION')
    document.setFont('Helvetica', 11)
    for position, text in enumerate((
        'This is synthetic test data. It is not a financial report.',
        'No OpenAI or xAI billing API was called.',
        f'Test namespace: {test_namespace}',
        f'Completed UTC date: {report_date.isoformat()}',
        'Synthetic amounts: OpenAI USD 0.00; xAI USD 0.00.',
    )):
        document.drawString(45, 740 - position * 22, text)
    document.showPage()
    document.save()
    record = {
        'data_type': 'SYNTHETIC_DELIVERY_TEST',
        'test_namespace': test_namespace,
        'report_date_utc': report_date.isoformat(),
        'currency': 'USD',
        'providers': {
            'openai': {'daily_usd': '0.00', 'synthetic': True},
            'xai': {'daily_usd': '0.00', 'synthetic': True},
        },
        'notes': ['TEST ONLY: no provider APIs were called; not actual billing data.'],
    }
    json_path.write_text(json.dumps(record, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False) + '\n',
                         encoding='utf-8')
    return pdf_path, json_path


def run_live_test(
    publisher: AppsScriptPublisher, report_date: date, pdf_path: Path,
    json_path: Path, test_namespace: str,
) -> dict[str, Any]:
    '''Confirm signed test delivery, retry identity and invalid-HMAC rejection.

    The guarded receiver validates TEST provenance and requires its dedicated
    test parent before any Drive operation. An older receiver rejects this
    request before Drive access; this helper never falls back to production.
    Response confirmations are distinct from independent Drive inspection.
    '''
    nonces: set[str] = set()

    def fresh_request() -> dict[str, Any]:
        '''Sign identical artifacts with fresh authentication for every attempt.'''
        envelope = build_test_envelope(report_date, pdf_path, json_path,
                                       publisher.secret, test_namespace)
        if envelope['nonce'] in nonces:
            raise AppsScriptDeliveryError('Test authentication nonce was unexpectedly repeated')
        nonces.add(envelope['nonce'])
        return envelope

    first = publisher.submit_test_envelope(fresh_request())
    second = publisher.submit_test_envelope(fresh_request())
    identity_fields = ('pdf', 'json', 'folder_id', 'test_namespace')
    if (second['duplicate'] is not True
            or any(second[field] != first[field] for field in identity_fields)):
        raise AppsScriptDeliveryError('Test retry did not confirm the same two files and folder')

    invalid = fresh_request()
    signature = invalid['signature']
    invalid['signature'] = ('0' if signature[0] != '0' else '1') + signature[1:]
    try:
        publisher.submit_test_envelope(invalid)
    except AppsScriptDeliveryError as error:
        if str(error) != 'Apps Script delivery rejected: invalid_signature':
            raise AppsScriptDeliveryError(
                'Invalid-HMAC test did not receive the expected invalid_signature rejection'
            ) from None
    else:
        raise AppsScriptDeliveryError('Receiver accepted an invalid-HMAC test request')

    after_rejection = publisher.submit_test_envelope(fresh_request())
    if (after_rejection['duplicate'] is not True
            or any(after_rejection[field] != first[field] for field in identity_fields)):
        raise AppsScriptDeliveryError('Test file identity changed after the rejected request')

    folder_id = first['folder_id']
    return {
        'status': 'api_confirmed',
        'successful_requests': 3,
        'rejected_requests': {'invalid_signature': 1},
        'duplicate_retry': 'same file IDs, names and hashes confirmed by receiver',
        'after_rejection': 'same file IDs, names and hashes confirmed by receiver',
        'independent_drive_inspection': 'not performed; inspect files in Drive as the owner',
        'test_namespace': test_namespace,
        'report_date_utc': report_date.isoformat(),
        'folder_id': folder_id,
        'folder_url': f'https://drive.google.com/drive/folders/{folder_id}',
        'file_ids': {'pdf': first['pdf'], 'json': first['json']},
    }


def main(argv: list[str] | None = None) -> int:
    '''Prepare offline by default; --live requires an environment-only secret.'''
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true',
                        help='Send TEST requests after manually enabling the isolated receiver')
    parser.add_argument('--url', default=os.environ.get('GOOGLE_APPS_SCRIPT_WEB_APP_URL')
                        or DEFAULT_WEB_APP_URL, help='Deployed Google Apps Script /exec URL')
    parser.add_argument('--namespace', default='test-' + secrets.token_hex(8),
                        help='Reuse this test- slug and date to repeat the same logical upload')
    parser.add_argument('--date', type=date.fromisoformat,
                        default=datetime.now(timezone.utc).date() - timedelta(days=1),
                        help='Completed UTC report day, YYYY-MM-DD; default yesterday UTC')
    args = parser.parse_args(argv)
    try:
        validate_web_app_url(args.url)
        secret = os.environ.get('GOOGLE_APPS_SCRIPT_SHARED_SECRET') if args.live else None
        if args.live and not secret:
            raise AppsScriptDeliveryError(
                'GOOGLE_APPS_SCRIPT_SHARED_SECRET is not available in the process environment'
            )
        # Offline signing uses an ephemeral synthetic key and never reads a real
        # environment secret. Neither mode prints or persists its signing key.
        signing_secret = secret if args.live else secrets.token_hex(32)
        with tempfile.TemporaryDirectory(prefix='ai-cost-reporter-test-') as temporary:
            pdf_path, json_path = generate_test_artifacts(Path(temporary), args.date,
                                                         args.namespace)
            envelope = build_test_envelope(args.date, pdf_path, json_path,
                                           signing_secret, args.namespace)
            summary: dict[str, Any] = {
                'status': 'prepared_offline',
                'network_requests': False,
                'report_date_utc': args.date.isoformat(),
                'test_namespace': args.namespace,
                'destination': ('AI_COST_TEST_PARENT_FOLDER_ID / AI Cost Reporter Tests / '
                                f'{args.namespace} / {args.date:%Y} / {args.date:%m}'),
                'files': [{key: item[key] for key in ('name', 'size', 'sha256')}
                          for item in envelope['files']],
                'local_artifacts': 'temporary; removed when this command exits',
            }
            if args.live:
                with requests.Session() as session:
                    publisher = AppsScriptPublisher(session, args.url, signing_secret)
                    summary.update(run_live_test(publisher, args.date, pdf_path,
                                                 json_path, args.namespace))
                summary['network_requests'] = True
            print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except AppsScriptDeliveryError as error:
        print(f'TEST FAILED: {error}', file=sys.stderr)
        return 1
    except OSError:
        print('TEST FAILED: synthetic temporary artifacts could not be created', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
