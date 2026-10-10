'''Inspect actual provider-recorded costs without writing or delivering reports.'''

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


class BillingSmokeError(RuntimeError):
    '''A validation or transport restriction that must stop the smoke read.'''


class SafeArgumentParser(argparse.ArgumentParser):
    '''Keep invalid arguments out of terminal error messages.'''

    def error(self, message: str) -> None:
        '''Reject invalid CLI input without echoing an accidentally supplied secret.'''
        raise BillingSmokeError('Invalid arguments')


class ReadOnlyBillingSession(requests.Session):
    '''Permit only the two documented billing reads, with strict HTTPS handling.'''

    def __init__(self, xai_team_id: str) -> None:
        '''Create a fresh session without ambient proxy, netrc or authentication state.'''
        if not isinstance(xai_team_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', xai_team_id) is None:
            raise BillingSmokeError('Invalid billing scope')
        super().__init__()
        self.trust_env = False
        self.allowed_requests = {
            ('GET', app.OPENAI_COSTS_URL),
            ('POST', f'{app.XAI_BASE_URL}/v1/billing/teams/{xai_team_id}/usage'),
        }

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        '''Reject writes, other endpoints and redirects before any credential forwarding.'''
        if (not isinstance(method, str) or not isinstance(url, str)
                or (method.upper(), url) not in self.allowed_requests):
            raise BillingSmokeError('A request outside the read-only billing scope was refused')
        kwargs['allow_redirects'] = False
        kwargs['verify'] = True
        response = super().request(method, url, **kwargs)
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
             'purpose': 'Read-only billing analytics query'},
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
    return {
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


def main(argv: list[str] | None = None) -> int:
    '''Print an offline plan by default, or a separately authorized read with --live.'''
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true',
                        help='Read both billing APIs using protected process environment values')
    parser.add_argument('--date', help='Completed UTC day, YYYY-MM-DD; default yesterday UTC')
    try:
        args = parser.parse_args(argv)
        report_date = (date.fromisoformat(args.date) if args.date else
                       datetime.now(timezone.utc).date() - timedelta(days=1))
        app.validate_billing_date(report_date)
        result = read_billing_costs(report_date) if args.live else offline_plan(report_date)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except Exception:
        # Provider/transport/config exceptions can contain secrets. Deliberately
        # report only a fixed failure code; no partial totals or traceback.
        print('BILLING_SMOKE_FAILED: no complete cost read was confirmed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
