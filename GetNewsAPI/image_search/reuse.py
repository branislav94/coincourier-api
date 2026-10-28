from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mysql.connector

from config import (
    STOCK_IMAGE_REUSE_CHECK_WP_HISTORY,
    STOCK_IMAGE_REUSE_WINDOW_DAYS,
    STOCK_IMAGE_USAGE_PATH,
    WP_DB_CONFIG,
)

from .downloader import perceptual_hash_distance
from .commons_identity import commons_identities, commons_identities_from_text
from .models import DownloadedImage, ImageCandidate


logger = logging.getLogger(__name__)
PHASH_REUSE_DISTANCE = 5


def _usage_path() -> Path:
    return Path(STOCK_IMAGE_USAGE_PATH)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_used_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _image_url_hash(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def prune_image_usage(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = _utc_now() - timedelta(days=STOCK_IMAGE_REUSE_WINDOW_DAYS)
    return [
        entry
        for entry in entries
        if (_parse_used_at(str(entry.get("used_at") or "")) or datetime.min.replace(tzinfo=timezone.utc))
        >= cutoff
    ]


def load_image_usage() -> list[dict[str, Any]]:
    path = _usage_path()
    try:
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return prune_image_usage([entry for entry in payload if isinstance(entry, dict)])
    except Exception:
        logger.exception("[IMG-V2] image usage read failed path=%s", path)
        return []


def _write_image_usage(entries: list[dict[str, Any]]) -> None:
    path = _usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=True, indent=2), encoding="utf-8")
        temporary.replace(path)
    except Exception:
        logger.exception("[IMG-V2] image usage write failed path=%s", path)


def record_image_usage(meta: dict[str, Any], post_id: int, title: str) -> None:
    provider = str(meta.get("provider") or "").lower()
    if provider not in {"pexels", "pixabay", "openverse", "wikimedia"}:
        return
    asset_id = str(meta.get("asset_id") or meta.get("provider_asset_id") or "")
    source_page = str(meta.get("source_page_url") or meta.get("credit_url") or "")
    image_url = str(meta.get("image_url") or "")
    entry = {
        "provider": provider,
        "asset_id": asset_id,
        "asset_key": meta.get("asset_key") or (f"{provider}:{asset_id}" if asset_id else ""),
        "canonical_source": meta.get("canonical_source") or "",
        "identity_aliases": sorted(_string_aliases(meta.get("identity_aliases"))),
        "source_page_url": source_page,
        "image_url_hash": meta.get("image_url_hash") or _image_url_hash(image_url),
        "content_sha256": meta.get("content_sha256") or "",
        "perceptual_hash": meta.get("perceptual_hash") or "",
        "creator_name": meta.get("creator_name") or meta.get("credit_name") or "",
        "creator_url": meta.get("creator_url") or "",
        "license_name": meta.get("license_name") or "",
        "license_version": meta.get("license_version") or "",
        "license_url": meta.get("license_url") or "",
        "attribution_text": meta.get("attribution_text") or "",
        "query": meta.get("query") or "",
        "score": meta.get("score"),
        "post_id": post_id,
        "title": title,
        "used_at": _utc_now().isoformat().replace("+00:00", "Z"),
        # Preserve fields written and read by V1 during the rollback window.
        "provider_asset_id": asset_id,
        "credit_url": source_page,
        "credit_name": meta.get("creator_name") or meta.get("credit_name") or "",
    }
    entries = prune_image_usage(load_image_usage())
    entries.append(entry)
    _write_image_usage(entries)
    logger.info("[IMG-V2] recorded image usage provider=%s asset_key=%s post_id=%s", provider, entry["asset_key"], post_id)


def _string_aliases(value: Any) -> set[str]:
    return {item for item in value if isinstance(item, str) and item} if isinstance(value, (list, tuple, set)) else set()


def candidate_identity_values(candidate: ImageCandidate) -> set[str]:
    values = {
        candidate.asset_key, candidate.canonical_source, candidate.source_page_url,
        candidate.usable_url, candidate.url_hash,
    } | _string_aliases(candidate.identity_aliases)
    values.discard("")
    source = candidate.metadata.get("source") or ("wikimedia" if candidate.provider == "wikimedia" else "")
    foreign_id = candidate.metadata.get("foreign_identifier") or (candidate.asset_id if candidate.provider == "wikimedia" else "")
    return values | commons_identities(*values, source=source, foreign_identifier=foreign_id)


def _history_identity_values(entry: dict[str, Any]) -> set[str]:
    values = {
        str(entry.get(key) or "")
        for key in ("asset_key", "canonical_source", "source_page_url", "credit_url", "image_url")
    } | _string_aliases(entry.get("identity_aliases"))
    values.discard("")
    source = entry.get("source") or ("wikimedia" if entry.get("provider") == "wikimedia" else "")
    foreign_id = entry.get("foreign_identifier") or (entry.get("asset_id") if entry.get("provider") == "wikimedia" else "")
    return values | commons_identities(*values, source=source, foreign_identifier=foreign_id)


def candidate_recent_local_usage(
    candidate: ImageCandidate,
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidate_hash = _image_url_hash(candidate.usable_url)
    identities = candidate_identity_values(candidate)
    for entry in entries:
        if identities & _history_identity_values(entry):
            return entry
        if entry.get("image_url_hash") and entry.get("image_url_hash") == candidate_hash:
            return entry
    return None


def downloaded_recent_local_usage(
    downloaded: DownloadedImage,
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("content_sha256") and entry.get("content_sha256") == downloaded.content_sha256:
            return entry
        distance = perceptual_hash_distance(
            str(entry.get("perceptual_hash") or ""),
            downloaded.perceptual_hash,
        )
        if distance is not None and distance <= PHASH_REUSE_DISTANCE:
            return entry
    return None


def candidate_recent_wp_usage(
    candidate: ImageCandidate,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    terms = candidate_identity_values(candidate)
    commons_terms = commons_identities(*terms)
    for row in rows:
        blob = " ".join(str(row.get(key) or "") for key in ("post_excerpt", "post_content", "guid"))
        # Commons comparisons use parsed complete identities rather than numeric
        # substrings (wikimedia:12 must not match wikimedia:123).
        if commons_terms & commons_identities_from_text(blob):
            return row
        ordinary_terms = {
            term for term in terms
            if not commons_identities(term)
            and not (commons_terms and term[:5].lower() == "file:")
        }
        if any(term in blob for term in ordinary_terms):
            return row
    return None


def _get_wp_prefix(conn: Any) -> str:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name LIKE %s LIMIT 1",
            ("%options",),
        )
        row = cursor.fetchone()
        return row[0].replace("options", "") if row else "wp_"
    finally:
        cursor.close()


def load_recent_wp_image_usage() -> list[dict[str, Any]]:
    if not STOCK_IMAGE_REUSE_CHECK_WP_HISTORY:
        return []
    cutoff = (_utc_now() - timedelta(days=STOCK_IMAGE_REUSE_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with mysql.connector.connect(**WP_DB_CONFIG) as conn:
            prefix = _get_wp_prefix(conn)
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute(
                    f"SELECT ID, post_date_gmt, post_excerpt, post_content, guid "
                    f"FROM `{prefix}posts` WHERE post_type = 'attachment' AND post_date_gmt >= %s",
                    (cutoff,),
                )
                return list(cursor.fetchall())
            finally:
                cursor.close()
    except Exception:
        logger.warning("[IMG-V2] WP-history reuse check failed; using local image usage only", exc_info=True)
        return []
