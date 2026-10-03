"""Load deterministic, versioned reviewed relationship labels."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from semantic_retrieval.evaluation import RelationshipLabel

from .models import LABEL_SCHEMA_VERSION, ReviewedLabel, ReviewedLabelSet


def _reviewed_label(raw: Any) -> ReviewedLabel:
    if not isinstance(raw, dict):
        raise ValueError("reviewed label entries must be objects")
    try:
        label = RelationshipLabel(str(raw.get("relationship_label") or ""))
    except ValueError as exc:
        raise ValueError("reviewed relationship label is invalid") from exc
    return ReviewedLabel(
        query_source_article_id=int(raw.get("query_source_article_id", 0)),
        candidate_source_article_id=int(raw.get("candidate_source_article_id", 0)),
        relationship_label=label,
        reviewer_note=(str(raw["reviewer_note"]) if raw.get("reviewer_note") else None),
        reviewed_at=(str(raw["reviewed_at"]) if raw.get("reviewed_at") else None),
    )


def _label_set(schema_version: str, rows: Iterable[Any]) -> ReviewedLabelSet:
    relationships = tuple(
        sorted(
            (_reviewed_label(row) for row in rows),
            key=lambda item: (
                item.query_source_article_id,
                item.candidate_source_article_id,
                item.relationship_label.value,
            ),
        )
    )
    return ReviewedLabelSet(
        schema_version=schema_version,
        relationships=relationships,
    )


def _load_json(path: Path) -> ReviewedLabelSet:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("reviewed-label JSON must be an object")
    rows = payload.get("relationships")
    if not isinstance(rows, list):
        raise ValueError("reviewed-label JSON requires a relationships array")
    return _label_set(str(payload.get("schema_version") or ""), rows)


def _load_jsonl(path: Path) -> ReviewedLabelSet:
    rows = []
    schema_versions = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"reviewed-label JSONL line {line_number} must be an object")
        schema_versions.add(str(raw.pop("schema_version", "")))
        rows.append(raw)
    if schema_versions != {LABEL_SCHEMA_VERSION}:
        raise ValueError("reviewed-label JSONL must use one supported schema version")
    return _label_set(LABEL_SCHEMA_VERSION, rows)


def _load_csv(path: Path) -> ReviewedLabelSet:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    schema_versions = {str(row.pop("schema_version", "")) for row in rows}
    if schema_versions != {LABEL_SCHEMA_VERSION}:
        raise ValueError("reviewed-label CSV must use one supported schema version")
    return _label_set(LABEL_SCHEMA_VERSION, rows)


def load_reviewed_labels(path: str | Path) -> ReviewedLabelSet:
    label_path = Path(path)
    suffix = label_path.suffix.lower()
    if suffix == ".json":
        return _load_json(label_path)
    if suffix == ".jsonl":
        return _load_jsonl(label_path)
    if suffix == ".csv":
        return _load_csv(label_path)
    raise ValueError("reviewed labels must use .json, .jsonl, or .csv")
