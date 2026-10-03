"""Deterministic JSON rendering for calibration reports."""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import Enum
from typing import Any

from .models import CalibrationReport


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def report_as_dict(report: CalibrationReport) -> dict[str, Any]:
    return _json_value(asdict(report))


def render_report_json(report: CalibrationReport) -> str:
    return json.dumps(
        report_as_dict(report),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
