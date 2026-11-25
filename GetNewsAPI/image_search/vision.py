"""Bounded Google Label Detection for prepared emergency stock crops."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from PIL import Image
from urllib3.exceptions import ReadTimeoutError


logger = logging.getLogger(__name__)

_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
_FEATURE = "LABEL_DETECTION"
_MAX_LABELS = 10
_MAX_LABEL_CHARS = 72
_PREVIEW_EDGE = 768
_PREVIEW_BYTES = 256 * 1024
_TIMEOUT = (2.0, 3.0)
_ARTICLE_UNIT_LIMIT = 2
_MAX_MONTHLY_UNITS = 2000
_IN_FLIGHT_SECONDS = 24 * 60 * 60
_FAILED_SECONDS = 15 * 60
_CACHE_SECONDS = 90 * 24 * 60 * 60
_CACHE_ROWS = 4096
_STORE_FILENAME = "stock_image_vision.sqlite3"

Labels = tuple[tuple[str, float], ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_preview(prepared_bytes: bytes) -> bytes | None:
    """Downsize the prepared crop to a metadata-free, bounded RGB JPEG."""
    try:
        with Image.open(io.BytesIO(prepared_bytes)) as image:
            preview = image.convert("RGB")
            preview.thumbnail((_PREVIEW_EDGE, _PREVIEW_EDGE), Image.Resampling.LANCZOS)
            preview.info.clear()
            for quality in (82, 65, 45):
                output = io.BytesIO()
                preview.save(output, format="JPEG", quality=quality)
                if output.tell() <= _PREVIEW_BYTES:
                    return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return None


def _label(description: object, score: object) -> tuple[str, float] | None:
    if not isinstance(description, str) or len(description) > 2 * _MAX_LABEL_CHARS:
        return None
    if isinstance(score, bool) or not isinstance(score, (float, int)):
        return None
    if not 0 <= score <= 1 or not math.isfinite(score):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", description).casefold().split())
    if len(normalized) > _MAX_LABEL_CHARS or not re.fullmatch(r"[a-z0-9]+(?:[ '\-][a-z0-9]+)*", normalized):
        return None
    return normalized, float(score)


def _request_labels(preview: bytes, api_key: str) -> tuple[Labels, str]:
    try:
        # The one-off Requests session uses the default adapter with zero retries.
        response = requests.post(
            _ENDPOINT,
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key},
            json={"requests": [{
                "image": {"content": base64.b64encode(preview).decode("ascii")},
                "features": [{"type": _FEATURE, "maxResults": _MAX_LABELS}],
            }]},
            timeout=_TIMEOUT,
            verify=True,
            allow_redirects=False,
        )
    except requests.exceptions.Timeout:
        return (), "timeout"
    except requests.exceptions.RequestException as error:
        # Requests wraps a timeout while reading the response body in ConnectionError.
        if error.args and isinstance(error.args[0], ReadTimeoutError):
            return (), "timeout"
        return (), "transport_error"

    status = response.status_code
    if type(status) is not int or not 100 <= status <= 599:
        return (), "malformed_response"
    if status != 200:
        return (), f"http_{status}"
    try:
        payload = response.json()
    except (ValueError, RecursionError):
        return (), "malformed_response"
    results = payload.get("responses") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return (), "malformed_response"
    result = results[0]
    if "error" in result:
        return (), "image_error"
    annotations = result.get("labelAnnotations", [])
    if not isinstance(annotations, list):
        return (), "malformed_response"

    labels = []
    for annotation in annotations[:_MAX_LABELS]:
        if isinstance(annotation, dict):
            label = _label(annotation.get("description"), annotation.get("score"))
            if label is not None:
                labels.append(label)
    return tuple(labels), ""


def _cached_labels(value: str) -> Labels:
    entries = json.loads(value)
    if not isinstance(entries, list) or len(entries) > _MAX_LABELS:
        raise ValueError("Invalid Vision cache evidence")
    labels = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError("Invalid Vision cache label")
        label = _label(*entry)
        if label is None:
            raise ValueError("Invalid Vision cache label")
        labels.append(label)
    return tuple(labels)


@dataclass(frozen=True)
class _Acquisition:
    status: str
    reason: str = ""
    labels: Labels = ()
    reservation: str = ""


class _VisionStore:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / _STORE_FILENAME

    def acquire(
        self, key: str, monthly_limit: int, article_id: str, submission_skip_reason: str,
    ) -> _Acquisition:
        with closing(sqlite3.connect(self.path, timeout=0.25)) as db, db:
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS vision_monthly_usage (
                month TEXT PRIMARY KEY,
                units INTEGER NOT NULL CHECK (units BETWEEN 0 AND 2000)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS vision_article_usage (
                article_id TEXT PRIMARY KEY,
                units INTEGER NOT NULL CHECK (units BETWEEN 0 AND 2)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS vision_observations (
                preview_key TEXT PRIMARY KEY,
                reservation TEXT NOT NULL,
                reserved_at REAL NOT NULL,
                completed_at REAL,
                labels TEXT CHECK (labels IS NULL OR completed_at IS NOT NULL)
            )""")

            now = _utc_now()
            timestamp = now.timestamp()
            row = db.execute("""SELECT reserved_at, completed_at, labels
                FROM vision_observations WHERE preview_key = ?""", (key,)).fetchone()
            if row is not None:
                reserved_at, completed_at, labels_json = row
                if completed_at is None and reserved_at > timestamp - _IN_FLIGHT_SECONDS:
                    return _Acquisition("in_flight", "duplicate_in_flight")
                if completed_at is not None and labels_json is not None and completed_at > timestamp - _CACHE_SECONDS:
                    return _Acquisition("cache_hit", labels=_cached_labels(labels_json))
                if completed_at is not None and completed_at > timestamp - _FAILED_SECONDS:
                    return _Acquisition("skipped", "recent_submission_failed")
            if submission_skip_reason:
                return _Acquisition("skipped", submission_skip_reason)
            if article_id:
                article_usage = db.execute(
                    "SELECT units FROM vision_article_usage WHERE article_id = ?", (article_id,),
                ).fetchone()
                if article_usage is not None and article_usage[0] >= _ARTICLE_UNIT_LIMIT:
                    return _Acquisition("skipped", "article_limit")

            month = now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")
            counter = db.execute("""INSERT INTO vision_monthly_usage (month, units) VALUES (?, 1)
                ON CONFLICT(month) DO UPDATE SET units = units + 1 WHERE units < ?""", (month, monthly_limit))
            if counter.rowcount == 0:
                return _Acquisition("skipped", "monthly_limit")
            if article_id:
                db.execute("""INSERT INTO vision_article_usage (article_id, units) VALUES (?, 1)
                    ON CONFLICT(article_id) DO UPDATE SET units = units + 1""", (article_id,))

            # Prune observations independently from the durable monthly counter.
            db.execute("""DELETE FROM vision_observations
                WHERE (completed_at IS NULL AND reserved_at <= ?)
                   OR (labels IS NULL AND completed_at <= ?)
                   OR (labels IS NOT NULL AND completed_at <= ?)""", (
                timestamp - _IN_FLIGHT_SECONDS, timestamp - _FAILED_SECONDS, timestamp - _CACHE_SECONDS,
            ))
            reservation = uuid.uuid4().hex
            db.execute("""INSERT OR REPLACE INTO vision_observations
                (preview_key, reservation, reserved_at) VALUES (?, ?, ?)""", (key, reservation, timestamp))
            return _Acquisition("submitted", reservation=reservation)

    def complete(self, key: str, reservation: str, labels: Labels | None) -> None:
        with closing(sqlite3.connect(self.path, timeout=0.25)) as db, db:
            db.execute("""UPDATE vision_observations SET completed_at = ?, labels = ?
                WHERE preview_key = ? AND reservation = ?""", (
                _utc_now().timestamp(), json.dumps(labels) if labels is not None else None, key, reservation,
            ))
            if labels is not None:
                db.execute("""DELETE FROM vision_observations WHERE preview_key IN (
                    SELECT preview_key FROM vision_observations WHERE labels IS NOT NULL
                    ORDER BY completed_at DESC, reservation DESC LIMIT -1 OFFSET ?
                )""", (_CACHE_ROWS,))


def _log_identifier(value: str) -> str:
    if not value:
        return "-"
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return f"sha256-{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:16]}"


@dataclass(frozen=True)
class VisionDecision:
    mode: str
    decision: str
    reason: str
    status: str
    units_reserved: int | None
    latency_ms: int
    checked_at_utc: str
    labels: Labels = ()

    @property
    def rejected(self) -> bool:
        return self.decision == "reject"

    def log(self, correlation: str, provider: str, asset_id: str) -> None:
        level = logging.WARNING if self.decision == "would_reject" else logging.INFO
        logger.log(
            level,
            "[IMG-VISION] at_utc=%s article=%s provider=%s asset_id=%s mode=%s decision=%s "
            "reason=%s status=%s units_reserved=%s latency_ms=%d labels=%s",
            self.checked_at_utc, _log_identifier(correlation), _log_identifier(provider),
            _log_identifier(asset_id), self.mode, self.decision, self.reason, self.status,
            self.units_reserved if self.units_reserved is not None else "unknown",
            self.latency_ms, json.dumps(self.labels, separators=(",", ":")),
        )


class VisionCheck:
    """An article-local gate over provider evidence and shared paid-unit accounting."""

    def __init__(
        self,
        mode: str,
        api_key: str | None,
        monthly_limit: int | None,
        state_dir: str | Path,
        conflict_rule: Callable[[Labels], str | None] | None = None,
        *,
        article_id: str = "",
    ):
        self.mode = mode if mode in {"off", "shadow", "enforce"} else "invalid"
        self.api_key = (api_key or "").strip()
        self.monthly_limit = monthly_limit
        self.state_dir = state_dir
        self.conflict_rule = conflict_rule
        self.article_id = article_id
        self._submitted_previews: set[str] = set()
        self._rejected_assets: set[tuple[str, str]] = set()
        self._rejected_previews: set[str] = set()

    def is_rejected_asset(self, provider: str, asset_id: str) -> bool:
        return (provider, asset_id) in self._rejected_assets

    def check(self, prepared_bytes: bytes, *, provider: str = "", asset_id: str = "") -> VisionDecision:
        started_at = time.monotonic()

        def finish(
            reason: str, *, decision: str = "skipped", status: str = "skipped", labels: Labels = (),
        ) -> VisionDecision:
            return VisionDecision(
                mode=self.mode, decision=decision, reason=reason, status=status,
                units_reserved=int(status == "submitted"), labels=labels,
                latency_ms=round((time.monotonic() - started_at) * 1000),
                checked_at_utc=_utc_now().isoformat(timespec="milliseconds"),
            )

        if self.mode == "off":
            return finish("mode_off")
        if self.mode == "invalid":
            return finish("invalid_mode")
        if self.conflict_rule is None:
            return finish("unsupported_topic")
        if not self.api_key:
            return finish("missing_credentials")
        if not re.fullmatch(r"[!-~]{1,256}", self.api_key):
            return finish("invalid_credentials")
        if type(self.monthly_limit) is not int or not 0 <= self.monthly_limit <= _MAX_MONTHLY_UNITS:
            return finish("invalid_monthly_limit")
        if self.monthly_limit == 0:
            return finish("monthly_limit_zero")
        if self.is_rejected_asset(provider, asset_id):
            return finish("duplicate_rejected_asset", decision="reject")
        preview = make_preview(prepared_bytes)
        if preview is None:
            return finish("preview_failed", decision="abstain")
        key = f"v1:{_FEATURE}:{_MAX_LABELS}:{hashlib.sha256(preview).hexdigest()}"
        if key in self._rejected_previews:
            if asset_id:
                self._rejected_assets.add((provider, asset_id))
            return finish("duplicate_rejected_crop", decision="reject")

        skip_submission = ""
        if key in self._submitted_previews:
            skip_submission = "recent_submission_failed"
        elif len(self._submitted_previews) >= _ARTICLE_UNIT_LIMIT:
            skip_submission = "article_limit"
        try:
            store = _VisionStore(self.state_dir)
            acquisition = store.acquire(key, self.monthly_limit, self.article_id, skip_submission)
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError, ZoneInfoNotFoundError):
            return finish("accounting_unavailable", decision="abstain")
        if acquisition.reason:
            decision = "abstain" if acquisition.reason == "recent_submission_failed" else "skipped"
            return finish(acquisition.reason, decision=decision, status=acquisition.status)

        labels = acquisition.labels
        if acquisition.reservation:
            self._submitted_previews.add(key)
            labels, error = _request_labels(preview, self.api_key)
            try:
                store.complete(key, acquisition.reservation, None if error else labels)
            except (OSError, sqlite3.Error):
                return finish(error or "accounting_unavailable", decision="abstain", status="submitted")
            if error:
                return finish(error, decision="abstain", status="submitted")

        conflict = self.conflict_rule(labels)
        if not conflict:
            return finish("insufficient_evidence", decision="abstain", status=acquisition.status, labels=labels)
        if self.mode == "shadow":
            return finish(conflict, decision="would_reject", status=acquisition.status, labels=labels)
        self._rejected_previews.add(key)
        if asset_id:
            self._rejected_assets.add((provider, asset_id))
        return finish(conflict, decision="reject", status=acquisition.status, labels=labels)
