"""Small publication policy helpers; no provider calls or long transactions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any, Mapping


QUOTA_EXHAUSTED = 'publication_quota_exhausted'
CAPACITY_UNAVAILABLE = 'publication_capacity_unavailable'
STALE_BACKLOG = 'stale_backlog_review_required'


def needs_freshness_review(row: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Keep old queued source material for review, without blocking existing posts."""
    if row.get('wp_post_id') is not None:
        return False
    value = row.get('source_publish_date') or row.get('selected_at')
    if value is None:
        # Missing provenance cannot prove that queued material is still fresh.
        return True
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return True
    if not isinstance(value, datetime):
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age_hours = int(os.getenv('SCHEDULE_MAX_AGE_HOURS', '36'))
    return value < current - timedelta(hours=age_hours)
