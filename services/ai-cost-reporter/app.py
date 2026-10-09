'''Daily provider-reported OpenAI and xAI API spending report.'''

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterator, Mapping

import requests
from filelock import FileLock, Timeout
from requests.adapters import HTTPAdapter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from urllib3.util.retry import Retry

from apps_script_delivery import AppsScriptDeliveryError, AppsScriptPublisher
from drive_delivery import DriveDeliveryError, DrivePublisher, refresh_access_token


OPENAI_COSTS_URL = 'https://api.openai.com/v1/organization/costs'
XAI_BASE_URL = 'https://management-api.x.ai'
NOTES = (
    'Provider-reported API consumption in USD, not a card-payment statement. '
    'Values can be adjusted after reporting; taxes, prepaid credit purchases, '
    'subscriptions and server hosting are not included. Totals use unrounded '
    'values; independently rounded lines can differ by a cent.'
)


class ReporterError(RuntimeError):
    '''An error that must prevent publication of an incomplete report.'''


@dataclass(frozen=True)
class ProviderSpend:
    '''Recorded spend for one provider over a day and the same month to date.'''

    daily: Decimal
    month_to_date: Decimal
    daily_breakdown: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class BillingReport:
    '''Provider-sourced daily spending values with reporting metadata.'''

    report_date: date
    openai: ProviderSpend
    xai: ProviderSpend
    generated_at: datetime
    demo: bool = False
    openai_project_ids: tuple[str, ...] = ()

    @property
    def daily_total(self) -> Decimal:
        '''Return the combined daily cost in USD.'''
        return self.openai.daily + self.xai.daily

    @property
    def month_to_date_total(self) -> Decimal:
        '''Return the combined month-to-date cost in USD.'''
        return self.openai.month_to_date + self.xai.month_to_date


def money(amount: Decimal) -> str:
    '''Format amounts to the cent only at presentation time.'''
    return f'${amount:,.2f}'


def decimal_value(value: object, context: str) -> Decimal:
    '''Convert a provider numeric value without floating-point accumulation.'''
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ReporterError(f'{context}: missing or invalid USD amount')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ReporterError(f'{context}: invalid USD amount') from error
    if not result.is_finite():
        raise ReporterError(f'{context}: non-finite USD amount')
    return result


def session_with_retry() -> requests.Session:
    '''Create a retrying HTTP session for read-only provider billing queries.'''
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({'GET', 'POST'}),
        respect_retry_after_header=True,
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def response_json(response: requests.Response, provider: str) -> Mapping[str, Any]:
    '''Validate an HTTP response without logging sensitive response bodies.'''
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise ReporterError(f'{provider} billing request failed: HTTP {response.status_code}') from error
    try:
        body = response.json()
    except ValueError as error:
        raise ReporterError(f'{provider} billing response was not JSON') from error
    if not isinstance(body, dict):
        raise ReporterError(f'{provider} billing response was not an object')
    return body


def fetch_openai_costs(
    session: requests.Session, admin_key: str, report_date: date,
    project_ids: tuple[str, ...] = (),
) -> ProviderSpend:
    '''Retrieve OpenAI month-to-date actual cost buckets, including pagination.'''
    month_start = report_date.replace(day=1)
    start = int(datetime(month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc).timestamp())
    end_day = report_date + timedelta(days=1)
    end = int(datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc).timestamp())
    params: dict[str, Any] = {
        'start_time': start,
        'end_time': end,
        'bucket_width': '1d',
        'group_by': ['line_item'],
        'limit': 180,
    }
    if project_ids:
        params['project_ids'] = list(project_ids)
    daily = Decimal('0')
    month_to_date = Decimal('0')
    breakdown: dict[str, Decimal] = {}
    seen_pages: set[str] = set()
    while True:
        try:
            response = session.get(
                OPENAI_COSTS_URL,
                headers={'Authorization': f'Bearer {admin_key}'},
                params=params,
                timeout=30,
            )
        except requests.RequestException as error:
            raise ReporterError('OpenAI billing request failed: network error') from error
        body = response_json(response, 'OpenAI')
        buckets = body.get('data')
        if not isinstance(buckets, list):
            raise ReporterError('OpenAI billing response omitted cost buckets')
        for bucket in buckets:
            if not isinstance(bucket, dict) or not isinstance(bucket.get('results'), list):
                raise ReporterError('OpenAI billing response contains an invalid bucket')
            bucket_timestamp = bucket.get('start_time')
            if isinstance(bucket_timestamp, bool) or not isinstance(bucket_timestamp, int):
                raise ReporterError('OpenAI billing bucket omitted its UTC start time')
            bucket_date = datetime.fromtimestamp(bucket_timestamp, timezone.utc).date()
            if not month_start <= bucket_date <= report_date:
                raise ReporterError('OpenAI returned a bucket outside the requested billing period')
            for result in bucket['results']:
                if not isinstance(result, dict) or not isinstance(result.get('amount'), dict):
                    raise ReporterError('OpenAI billing entry omitted its amount')
                amount = result['amount']
                if str(amount.get('currency', '')).lower() != 'usd':
                    raise ReporterError('OpenAI returned a non-USD billing amount')
                cost = decimal_value(amount.get('value'), 'OpenAI')
                month_to_date += cost
                if bucket_date == report_date:
                    daily += cost
                    label = str(result.get('line_item') or 'Uncategorized')
                    breakdown[label] = breakdown.get(label, Decimal('0')) + cost
        has_more = body.get('has_more')
        if not isinstance(has_more, bool):
            raise ReporterError('OpenAI billing response omitted pagination state')
        if not has_more:
            break
        page = body.get('next_page')
        if not isinstance(page, str) or not page or page in seen_pages:
            raise ReporterError('OpenAI billing pagination cursor is invalid')
        seen_pages.add(page)
        params['page'] = page
    return ProviderSpend(daily=daily, month_to_date=month_to_date, daily_breakdown=breakdown)


def fetch_xai_costs(
    session: requests.Session, management_key: str, team_id: str, report_date: date
) -> ProviderSpend:
    '''Retrieve xAI's billed USD usage series through its Management API.'''
    month_start = report_date.replace(day=1)
    payload = {
        'analyticsRequest': {
            'timeRange': {
                'startTime': f'{month_start.isoformat()} 00:00:00',
                'endTime': f'{report_date.isoformat()} 23:59:59',
                'timezone': 'Etc/GMT',
            },
            'timeUnit': 'TIME_UNIT_DAY',
            'values': [{'name': 'usd', 'aggregation': 'AGGREGATION_SUM'}],
            'groupBy': ['description'],
            'filters': [],
        }
    }
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', team_id):
        raise ReporterError('XAI_TEAM_ID has an invalid format')
    try:
        response = session.post(
            f'{XAI_BASE_URL}/v1/billing/teams/{team_id}/usage',
            headers={'Authorization': f'Bearer {management_key}'},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as error:
        raise ReporterError('xAI billing request failed: network error') from error
    body = response_json(response, 'xAI')
    if body.get('limitReached') is True:
        raise ReporterError('xAI billing analytics were truncated; refusing to report partial costs')
    if body.get('limitReached') is not False:
        raise ReporterError('xAI billing response omitted the completeness flag')
    series = body.get('timeSeries')
    if not isinstance(series, list):
        raise ReporterError('xAI billing response omitted usage time series')
    daily = Decimal('0')
    month_to_date = Decimal('0')
    breakdown: dict[str, Decimal] = {}
    for group in series:
        if not isinstance(group, dict) or not isinstance(group.get('dataPoints'), list):
            raise ReporterError('xAI returned an invalid usage group')
        group_names = group.get('groupLabels') or group.get('group') or []
        label = str(group_names[0]) if isinstance(group_names, list) and group_names else 'Uncategorized'
        for point in group['dataPoints']:
            if not isinstance(point, dict) or not isinstance(point.get('values'), list):
                raise ReporterError('xAI returned an invalid usage data point')
            values = point['values']
            if len(values) != 1:
                raise ReporterError('xAI billing series has an unexpected number of metrics')
            try:
                timestamp = str(point['timestamp']).replace('Z', '+00:00')
                point_time = datetime.fromisoformat(timestamp)
            except (ValueError, KeyError) as error:
                raise ReporterError('xAI billing usage timestamp is invalid') from error
            if point_time.tzinfo is None:
                raise ReporterError('xAI usage timestamps must include a timezone')
            point_date = point_time.astimezone(timezone.utc).date()
            if not month_start <= point_date <= report_date:
                raise ReporterError('xAI returned a day outside the requested billing period')
            cost = decimal_value(values[0], 'xAI')
            month_to_date += cost
            if point_date == report_date:
                daily += cost
                breakdown[label] = breakdown.get(label, Decimal('0')) + cost
    return ProviderSpend(daily=daily, month_to_date=month_to_date, daily_breakdown=breakdown)


def report_to_dict(report: BillingReport) -> dict[str, Any]:
    '''Create a machine-readable, explicitly sourced spending record.'''
    def provider_data(value: ProviderSpend) -> dict[str, Any]:
        '''Represent one provider with full monetary precision.'''
        return {
            'daily_usd': str(value.daily),
            'month_to_date_usd': str(value.month_to_date),
            'daily_breakdown_usd': {
                name: str(amount) for name, amount in sorted(value.daily_breakdown.items())
            },
        }

    return {
        'report_date_utc': report.report_date.isoformat(),
        'month_start_utc': report.report_date.replace(day=1).isoformat(),
        'generated_at_utc': report.generated_at.isoformat(),
        'currency': 'USD',
        'data_type': 'SYNTHETIC_DEMO' if report.demo else 'PROVIDER_REPORTED_API_SPEND',
        'openai_scope': {
            'type': 'selected_projects' if report.openai_project_ids else 'all_organization_projects',
            'project_ids': list(report.openai_project_ids),
        },
        'providers': {'openai': provider_data(report.openai), 'xai': provider_data(report.xai)},
        'daily_total_usd': str(report.daily_total),
        'month_to_date_total_usd': str(report.month_to_date_total),
        'notes': NOTES,
    }



def report_from_dict(data: Mapping[str, Any], expected_date: date) -> BillingReport:
    '''Reconstruct the original report for retrying a partially delivered snapshot.'''
    if data.get('data_type') != 'PROVIDER_REPORTED_API_SPEND':
        raise ReporterError('Stored report is not a live provider billing report')
    if data.get('report_date_utc') != expected_date.isoformat():
        raise ReporterError('Stored report date does not match this delivery')
    providers = data.get('providers')
    if not isinstance(providers, dict):
        raise ReporterError('Stored report has no provider data')

    def read_provider(name: str) -> ProviderSpend:
        '''Validate and decode one stored provider snapshot.'''
        record = providers.get(name)
        if not isinstance(record, dict):
            raise ReporterError(f'Stored report is missing {name} provider data')
        breakdown = record.get('daily_breakdown_usd')
        if not isinstance(breakdown, dict):
            raise ReporterError(f'Stored report {name} breakdown is invalid')
        return ProviderSpend(
            daily=decimal_value(record.get('daily_usd'), name),
            month_to_date=decimal_value(record.get('month_to_date_usd'), name),
            daily_breakdown={str(key): decimal_value(value, name)
                             for key, value in breakdown.items()},
        )

    try:
        created_at = datetime.fromisoformat(str(data['generated_at_utc']))
    except (KeyError, ValueError) as error:
        raise ReporterError('Stored report generation timestamp is invalid') from error
    if created_at.tzinfo is None:
        raise ReporterError('Stored report generation timestamp has no timezone')
    scope = data.get('openai_scope', {'type': 'all_organization_projects', 'project_ids': []})
    if not isinstance(scope, dict) or not isinstance(scope.get('project_ids'), list):
        raise ReporterError('Stored OpenAI billing scope is invalid')
    project_ids = scope['project_ids']
    if not all(isinstance(value, str) and re.fullmatch(r'proj_[A-Za-z0-9_-]{1,128}', value)
               for value in project_ids):
        raise ReporterError('Stored OpenAI project IDs are invalid')
    report = BillingReport(
        report_date=expected_date,
        openai=read_provider('openai'),
        xai=read_provider('xai'),
        generated_at=created_at,
        openai_project_ids=tuple(project_ids),
    )
    if decimal_value(data.get('daily_total_usd'), 'Stored total') != report.daily_total:
        raise ReporterError('Stored daily cost total disagrees with provider totals')
    if decimal_value(data.get('month_to_date_total_usd'), 'Stored MTD total') != report.month_to_date_total:
        raise ReporterError('Stored month-to-date total disagrees with provider totals')
    return report


def write_json_atomic(target: Path, data: Mapping[str, Any]) -> None:
    '''Persist JSON by atomic replacement so restarts do not leave partial files.'''
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            'w', dir=target.parent, prefix='.pending-', suffix='.json', encoding='utf-8', delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=True)
            handle.write('\n')
        os.replace(temp_path, target)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def breakdown_rows(values: Mapping[str, Decimal], max_rows: int = 7) -> list[list[str]]:
    '''Return a bounded table without silently losing smaller cost entries.'''
    ranked = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    rows = [[name[:95], money(value)] for name, value in ranked[:max_rows]]
    if len(ranked) > max_rows:
        rest_total = sum((value for _, value in ranked[max_rows:]), Decimal('0'))
        rows.append([f'Other ({len(ranked) - max_rows} line items)', money(rest_total)])
    if not rows:
        rows.append(['No recorded spend', '$0.00'])
    return rows


def create_pdf(report: BillingReport, output_path: Path) -> None:
    '''Produce a one- to two-page PDF without estimations or model-rate calculations.'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=19 * mm,
        leftMargin=19 * mm,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        title=f'AI API Cost Report {report.report_date.isoformat()}',
        author='AI Cost Reporter',
    )
    styles = getSampleStyleSheet()
    primary = colors.HexColor('#152339')
    muted = colors.HexColor('#516073')
    pale = colors.HexColor('#f2f5fa')
    styles.add(ParagraphStyle(
        name='TitleCustom', fontName='Helvetica-Bold', fontSize=18,
        leading=22, textColor=primary, spaceAfter=5, alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        name='SectionCustom', fontName='Helvetica-Bold', fontSize=11,
        leading=14, textColor=primary, spaceBefore=11, spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name='SmallCustom', fontName='Helvetica', fontSize=8,
        leading=12, textColor=muted,
    ))
    styles.add(ParagraphStyle(
        name='CellCustom', fontName='Helvetica', fontSize=8,
        leading=10, textColor=primary,
    ))
    styles.add(ParagraphStyle(
        name='AmountCustom', fontName='Helvetica-Bold', fontSize=8,
        leading=10, textColor=primary, alignment=TA_RIGHT,
    ))
    story: list[Any] = [
        Paragraph('DAILY AI API COST REPORT', styles['TitleCustom']),
        Paragraph(f'{report.report_date.isoformat()} (UTC)  |  USD  |  Provider billing records', styles['SmallCustom']),
        Paragraph(
            'OpenAI scope: ' + (
                html.escape(', '.join(report.openai_project_ids))
                if report.openai_project_ids else 'all organization projects'
            ) + '; xAI scope: configured team', styles['SmallCustom'],
        ),
        Spacer(1, 5 * mm),
    ]
    if report.demo:
        story.extend([
            Paragraph('SAMPLE ONLY - SYNTHETIC FIGURES - NOT ACTUAL SPENDING', styles['SectionCustom']),
            Spacer(1, 3 * mm),
        ])
    summary = [
        ['Provider', 'Yesterday (USD)', 'Month-to-date (USD)'],
        ['OpenAI', money(report.openai.daily), money(report.openai.month_to_date)],
        ['xAI / Grok', money(report.xai.daily), money(report.xai.month_to_date)],
        ['TOTAL', money(report.daily_total), money(report.month_to_date_total)],
    ]
    table = Table(summary, colWidths=[60 * mm, 53 * mm, 53 * mm], hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), primary),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BACKGROUND', (0, -1), (-1, -1), pale),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LINEBELOW', (0, 1), (-1, -2), 0.25, colors.HexColor('#d8dfe7')),
    ]))
    story.append(table)
    for title, spend in [('OpenAI - daily cost line items', report.openai),
                         ('xAI / Grok - daily usage descriptions', report.xai)]:
        data: list[list[Any]] = [[
            Paragraph('Description', styles['CellCustom']),
            Paragraph('USD', styles['AmountCustom']),
        ]]
        for label, amount in breakdown_rows(spend.daily_breakdown):
            data.append([
                Paragraph(html.escape(label), styles['CellCustom']),
                Paragraph(amount, styles['AmountCustom']),
            ])
        detail_table = Table(data, colWidths=[130 * mm, 36 * mm], hAlign='LEFT')
        detail_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), pale),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LINEBELOW', (0, 1), (-1, -1), 0.2, colors.HexColor('#e4e8ee')),
        ]))
        story.append(KeepTogether([Paragraph(title, styles['SectionCustom']), detail_table]))
    generated_label = report.generated_at.strftime('%Y-%m-%d %H:%M UTC')
    story.extend([
        Spacer(1, 6 * mm),
        HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#d8dfe7')),
        Spacer(1, 2 * mm),
        Paragraph(html.escape(NOTES), styles['SmallCustom']),
        Paragraph(
            f'Generated {generated_label}; '
            'daily boundaries: 00:00:00-23:59:59 UTC.',
            styles['SmallCustom'],
        ),
    ])
    doc.build(story)


def recipient_list(value: str) -> list[str]:
    '''Read a comma- or semicolon-separated list of notification recipients.'''
    return [entry.strip() for entry in re.split(r'[,;]', value) if entry.strip()]


def send_email(report: BillingReport, pdf_path: Path) -> None:
    '''Send an HTML email and attach the PDF through a TLS-protected SMTP relay.'''
    recipients = recipient_list(os.environ.get('EMAIL_TO', ''))
    if not recipients:
        return
    host = required_env('SMTP_HOST')
    sender = required_env('SMTP_FROM')
    security = os.environ.get('SMTP_SECURITY', 'starttls').strip().lower()
    if security not in {'starttls', 'ssl'}:
        raise ReporterError('SMTP_SECURITY must be starttls or ssl; plaintext transport is forbidden')
    port = int(os.environ.get('SMTP_PORT', '465' if security == 'ssl' else '587'))
    username = os.environ.get('SMTP_USERNAME', '')
    password = os.environ.get('SMTP_PASSWORD', '')
    if bool(username) != bool(password):
        raise ReporterError('SMTP_USERNAME and SMTP_PASSWORD must both be set or both be blank')
    email = EmailMessage()
    email['From'] = sender
    email['To'] = ', '.join(recipients)
    email['Subject'] = f'AI API spending | {report.report_date.isoformat()} UTC | {money(report.daily_total)}'
    plain = (
        f'AI API spending for {report.report_date.isoformat()} (UTC)\n\n'
        f'OpenAI: {money(report.openai.daily)}\n'
        f'xAI/Grok: {money(report.xai.daily)}\n'
        f'Daily total: {money(report.daily_total)}\n'
        f'Month-to-date: {money(report.month_to_date_total)}\n\n'
        f'{NOTES}\n\nFull report is attached as PDF.'
    )
    email.set_content(plain)
    email.add_alternative(
        '<h2>AI API spending</h2>'
        f'<p>Report date: <b>{report.report_date.isoformat()} (UTC)</b></p>'
        '<table cellpadding="6" cellspacing="0" border="1">'
        f'<tr><th>Provider</th><th>Daily</th><th>Month-to-date</th></tr>'
        f'<tr><td>OpenAI</td><td>{money(report.openai.daily)}</td>'
        f'<td>{money(report.openai.month_to_date)}</td></tr>'
        f'<tr><td>xAI / Grok</td><td>{money(report.xai.daily)}</td>'
        f'<td>{money(report.xai.month_to_date)}</td></tr>'
        f'<tr><th>Total</th><th>{money(report.daily_total)}</th>'
        f'<th>{money(report.month_to_date_total)}</th></tr></table>'
        f'<p><small>{html.escape(NOTES)}</small></p>',
        subtype='html',
    )
    email.add_attachment(pdf_path.read_bytes(), maintype='application', subtype='pdf', filename=pdf_path.name)
    try:
        context = ssl.create_default_context()
        if security == 'ssl':
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(email)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(email)
    except (OSError, smtplib.SMTPException) as error:
        raise ReporterError('Email delivery failed: SMTP transport error') from error


def send_whatsapp(
    session: requests.Session, report: BillingReport, pdf_path: Path, recipient: str
) -> None:
    '''Upload PDF and send it inside an approved WhatsApp document-header template.'''
    token = required_env('WHATSAPP_TOKEN')
    phone_id = required_env('WHATSAPP_PHONE_NUMBER_ID')
    template = os.environ.get('WHATSAPP_TEMPLATE_NAME', 'daily_ai_cost_report')
    language = os.environ.get('WHATSAPP_TEMPLATE_LANGUAGE', 'en_US')
    version = os.environ.get('WHATSAPP_API_VERSION', 'v25.0')
    if not re.fullmatch(r'v\d+\.\d+', version):
        raise ReporterError('WHATSAPP_API_VERSION must use the format v25.0')
    if not re.fullmatch(r'\d{5,24}', phone_id):
        raise ReporterError('WHATSAPP_PHONE_NUMBER_ID has an invalid format')
    if not re.fullmatch(r'\d{7,16}', recipient):
        raise ReporterError('WhatsApp recipient must be digits including country code')
    base = f'https://graph.facebook.com/{version}/{phone_id}'
    headers = {'Authorization': f'Bearer {token}'}
    try:
        with pdf_path.open('rb') as pdf:
            upload = session.post(
                f'{base}/media',
                headers=headers,
                data={'messaging_product': 'whatsapp', 'type': 'application/pdf'},
                files={'file': (pdf_path.name, pdf, 'application/pdf')},
                timeout=45,
            )
        upload_data = response_json(upload, 'WhatsApp media upload')
        media_id = upload_data.get('id')
        if not isinstance(media_id, str) or not media_id:
            raise ReporterError('WhatsApp upload response omitted its media identifier')
        payload = {
            'messaging_product': 'whatsapp',
            'to': recipient,
            'type': 'template',
            'template': {
                'name': template,
                'language': {'code': language},
                'components': [
                    {'type': 'header', 'parameters': [
                        {'type': 'document', 'document': {'id': media_id, 'filename': pdf_path.name}}
                    ]},
                    {'type': 'body', 'parameters': [
                        {'type': 'text', 'text': report.report_date.isoformat()},
                        {'type': 'text', 'text': f'{report.openai.daily:.2f}'},
                        {'type': 'text', 'text': f'{report.xai.daily:.2f}'},
                        {'type': 'text', 'text': f'{report.daily_total:.2f}'},
                    ]},
                ],
            },
        }
        message = session.post(f'{base}/messages', headers=headers, json=payload, timeout=30)
        result = response_json(message, 'WhatsApp send')
        if not isinstance(result.get('messages'), list) or not result['messages']:
            raise ReporterError('WhatsApp API did not accept the template message')
    except requests.RequestException as error:
        raise ReporterError('WhatsApp delivery failed: network error') from error


def required_env(name: str) -> str:
    '''Return a configured secret or other mandatory environment variable.'''
    value = os.environ.get(name, '').strip()
    if not value:
        raise ReporterError(f'Required environment variable {name} is not set')
    return value


def google_drive_enabled() -> bool:
    '''Read a boolean switch; reject misspellings rather than silently disabling uploads.'''
    value = os.environ.get('GOOGLE_DRIVE_ENABLED', '').strip().lower()
    if value not in ('', '0', 'false', 'no', '1', 'true', 'yes'):
        raise ReporterError('GOOGLE_DRIVE_ENABLED must be true/false or 1/0')
    return value in ('1', 'true', 'yes')


def google_drive_backend() -> str:
    '''Select delivery explicitly while preserving existing OAuth installations.'''
    backend = os.environ.get('GOOGLE_DRIVE_BACKEND', 'oauth').strip().lower()
    if backend not in {'oauth', 'apps_script'}:
        raise ReporterError('GOOGLE_DRIVE_BACKEND must be oauth or apps_script')
    return backend


def google_drive_target(backend: str) -> str:
    '''Identify the destination for markers without storing credentials or URLs.'''
    target = (os.environ.get('GOOGLE_APPS_SCRIPT_WEB_APP_URL', '').strip()
              if backend == 'apps_script'
              else os.environ.get('GOOGLE_DRIVE_FOLDER_NAME', 'AI Infrastructure Costs'))
    return hashlib.sha256(target.encode('utf-8')).hexdigest()


def validate_delivery_configuration() -> tuple[bool, list[str], bool]:
    '''Fail fast on missing delivery settings before querying billing APIs.'''
    email_enabled = bool(recipient_list(os.environ.get('EMAIL_TO', '')))
    wa_recipients = recipient_list(os.environ.get('WHATSAPP_TO', ''))
    drive_enabled = google_drive_enabled()
    if not email_enabled and not wa_recipients and not drive_enabled:
        raise ReporterError('Configure GOOGLE_DRIVE_ENABLED, EMAIL_TO or WHATSAPP_TO, or use --no-send')
    if email_enabled:
        required_env('SMTP_HOST')
        required_env('SMTP_FROM')
    if wa_recipients:
        required_env('WHATSAPP_TOKEN')
        required_env('WHATSAPP_PHONE_NUMBER_ID')
    if drive_enabled:
        if google_drive_backend() == 'apps_script':
            try:
                with requests.Session() as script_session:
                    AppsScriptPublisher(
                        script_session, required_env('GOOGLE_APPS_SCRIPT_WEB_APP_URL'),
                        required_env('GOOGLE_APPS_SCRIPT_SHARED_SECRET'),
                    )
            except AppsScriptDeliveryError as error:
                raise ReporterError(str(error)) from error
        else:
            required_env('GOOGLE_OAUTH_CLIENT_ID')
            required_env('GOOGLE_OAUTH_CLIENT_SECRET')
            required_env('GOOGLE_OAUTH_REFRESH_TOKEN')
    return email_enabled, wa_recipients, drive_enabled


@contextmanager
def exclusive_lock(output_dir: Path) -> Iterator[None]:
    '''Prevent manual and scheduled executions from sending duplicate messages concurrently.'''
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(output_dir / '.daily-report.lock'))
    try:
        with lock.acquire(timeout=0):
            yield
    except Timeout as error:
        raise ReporterError('Another report execution is already running') from error


def demo_report(report_date: date) -> BillingReport:
    '''Return clearly labeled synthetic billing data without contacting providers.'''
    return BillingReport(
        report_date=report_date,
        openai=ProviderSpend(
            daily=Decimal('4.2371'), month_to_date=Decimal('53.104'),
            daily_breakdown={'Completions': Decimal('3.986'), 'Web searches': Decimal('0.2511')},
        ),
        xai=ProviderSpend(
            daily=Decimal('2.6178'), month_to_date=Decimal('29.488'),
            daily_breakdown={'Chat grok example': Decimal('2.6178')},
        ),
        generated_at=datetime.now(timezone.utc),
        demo=True,
    )


def get_live_report(session: requests.Session, report_date: date) -> BillingReport:
    '''Fetch both providers; require both to succeed before any output is delivered.'''
    openai_key = required_env('OPENAI_ADMIN_KEY')
    xai_key = required_env('XAI_MANAGEMENT_KEY')
    team_id = required_env('XAI_TEAM_ID')
    raw_project_ids = recipient_list(os.environ.get('OPENAI_PROJECT_IDS', ''))
    if not all(re.fullmatch(r'proj_[A-Za-z0-9_-]{1,128}', value) for value in raw_project_ids):
        raise ReporterError('OPENAI_PROJECT_IDS must be comma-separated project IDs')
    project_ids = tuple(dict.fromkeys(raw_project_ids))
    openai = fetch_openai_costs(session, openai_key, report_date, project_ids)
    xai = fetch_xai_costs(session, xai_key, team_id, report_date)
    return BillingReport(
        report_date=report_date,
        openai=openai,
        xai=xai,
        generated_at=datetime.now(timezone.utc),
        openai_project_ids=project_ids,
    )


def run_report(
    report_date: date, output_dir: Path, demo: bool, no_send: bool, force_resend: bool
) -> Path:
    '''Generate a report and deliver to each configured channel at most once per date.'''
    if report_date >= datetime.now(timezone.utc).date() and not demo:
        raise ReporterError('Report date must be a fully completed UTC billing day')
    email_enabled = False
    wa_recipients: list[str] = []
    drive_enabled = False
    if not demo and not no_send:
        email_enabled, wa_recipients, drive_enabled = validate_delivery_configuration()
    backend = google_drive_backend() if drive_enabled else 'oauth'
    target = google_drive_target(backend) if drive_enabled else ''
    if drive_enabled and backend == 'apps_script' and report_date < date(2000, 1, 1):
        raise ReporterError('Apps Script report date must be from 2000 onward')
    with exclusive_lock(output_dir):
        suffix = '-DEMO' if demo else ''
        name = f'ai-cost-report-{report_date.isoformat()}{suffix}'
        pdf_path = output_dir / f'{name}.pdf'
        json_path = output_dir / f'{name}.json'
        delivery_path = output_dir / f'{name}.delivery.json'
        if no_send and delivery_path.exists():
            raise ReporterError('Use a separate --output-dir for --no-send; this delivery snapshot is protected')
        status: dict[str, Any] = {
            'email': False, 'whatsapp': [], 'google_drive': False,
            'google_drive_backend': backend, 'google_drive_target': target,
            'google_drive_replace': force_resend,
        }
        if delivery_path.exists() and not force_resend and not demo and not no_send:
            existing = json.loads(delivery_path.read_text(encoding='utf-8'))
            if not isinstance(existing, dict):
                raise ReporterError('Existing delivery marker is not a JSON object')
            sent_to = existing.get('whatsapp', [])
            if not isinstance(sent_to, list) or not all(isinstance(v, str) for v in sent_to):
                raise ReporterError('Existing delivery marker has invalid recipients')
            same_drive_target = (
                existing.get('google_drive_backend', 'oauth') == backend
                and (existing.get('google_drive_target') == target
                     or (backend == 'oauth' and 'google_drive_target' not in existing))
            )
            status = {
                'email': existing.get('email') is True,
                'whatsapp': sent_to,
                'google_drive': existing.get('google_drive') is True and same_drive_target,
                'google_drive_backend': backend, 'google_drive_target': target,
                'google_drive_replace': existing.get('google_drive_replace') is True and same_drive_target,
            }
            if status['google_drive'] and isinstance(existing.get('google_drive_file_ids'), dict):
                status['google_drive_file_ids'] = existing['google_drive_file_ids']
            if not pdf_path.is_file() or not json_path.is_file():
                raise ReporterError('Original report snapshot is missing; use --force-resend to regenerate')
            if ((not email_enabled or status['email'])
                and (not drive_enabled or status['google_drive'])
                and all(recipient in status['whatsapp'] for recipient in wa_recipients)):
                print('Report was already sent to all configured destinations; no repeat billing queries')
                return pdf_path
        session = session_with_retry()
        try:
            if delivery_path.exists() and not force_resend and not demo and not no_send:
                report = report_from_dict(
                    json.loads(json_path.read_text(encoding='utf-8')), report_date
                )
                print('Reusing the original PDF snapshot for unfinished delivery')
            else:
                report = demo_report(report_date) if demo else get_live_report(session, report_date)
                create_pdf(report, pdf_path)
                write_json_atomic(json_path, report_to_dict(report))
            print(f'Report created: {pdf_path.name}; daily total: {money(report.daily_total)}')
            if demo or no_send:
                print('Delivery disabled: demo or --no-send mode')
                return pdf_path
            # Create a marker *before* the first external send. If the first
            # upload fails, a retry must reuse this exact PDF/JSON snapshot.
            if force_resend or not delivery_path.exists():
                write_json_atomic(delivery_path, status)
            if drive_enabled and not status['google_drive']:
                try:
                    with requests.Session() as drive_session:
                        if backend == 'apps_script':
                            drive_session.trust_env = False
                            uploaded = AppsScriptPublisher(
                                drive_session, required_env('GOOGLE_APPS_SCRIPT_WEB_APP_URL'),
                                required_env('GOOGLE_APPS_SCRIPT_SHARED_SECRET'),
                            ).publish(
                                report_date, pdf_path, json_path,
                                replace=force_resend or status['google_drive_replace'],
                            )
                        else:
                            token = refresh_access_token(
                                drive_session,
                                required_env('GOOGLE_OAUTH_CLIENT_ID'),
                                required_env('GOOGLE_OAUTH_CLIENT_SECRET'),
                                required_env('GOOGLE_OAUTH_REFRESH_TOKEN'),
                            )
                            uploaded = DrivePublisher(drive_session, token).publish(
                                report_date, pdf_path, json_path,
                                os.environ.get('GOOGLE_DRIVE_FOLDER_NAME', 'AI Infrastructure Costs'),
                                replace=force_resend or status['google_drive_replace'],
                            )
                except (DriveDeliveryError, AppsScriptDeliveryError) as error:
                    raise ReporterError(str(error)) from error
                status['google_drive'] = True
                status['google_drive_replace'] = False
                status['google_drive_file_ids'] = uploaded
                write_json_atomic(delivery_path, status)
                print('Google Drive accepted PDF and JSON report uploads')
            if email_enabled and not status['email']:
                send_email(report, pdf_path)
                status['email'] = True
                write_json_atomic(delivery_path, status)
                print('Email accepted by SMTP relay')
            for recipient in wa_recipients:
                if recipient not in status['whatsapp']:
                    send_whatsapp(session, report, pdf_path, recipient)
                    status['whatsapp'].append(recipient)
                    write_json_atomic(delivery_path, status)
                    print(f'WhatsApp API accepted report for ...{recipient[-4:]}')
            if (email_enabled and status['email'] or wa_recipients and status['whatsapp']
                    or drive_enabled and status['google_drive']):
                print('Delivery state saved; reruns skip already accepted destinations')
            return pdf_path
        finally:
            session.close()


def parse_args() -> argparse.Namespace:
    '''Parse CLI options for Dokploy schedule jobs and local verification.'''
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, default=None,
                        help='UTC billing date (YYYY-MM-DD); defaults to yesterday UTC')
    parser.add_argument('--output-dir', type=Path,
                        default=Path(os.environ.get('OUTPUT_DIR', '/data/reports')))
    parser.add_argument('--no-send', action='store_true', help='Generate files without notifications')
    parser.add_argument('--demo', action='store_true',
                        help='Synthetic PDF and JSON only; no provider calls or notifications')
    parser.add_argument('--force-resend', action='store_true',
                        help='Resend to all destinations despite previously saved delivery markers')
    return parser.parse_args()


def main() -> int:
    '''Execute one reporting cycle with predictable nonzero failure exit codes.'''
    args = parse_args()
    report_date = args.date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    try:
        run_report(report_date, args.output_dir, args.demo, args.no_send, args.force_resend)
    except (ReporterError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
