"""Isolated WordPress postmeta persistence used for idempotency."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .seo import get_wp_prefix


PUBLICATION_KEY_META = "_coincourier_publication_key"
RAW_ARTICLE_ID_META = "_coincourier_raw_article_id"
RICH_ARTICLE_ID_META = "_coincourier_rich_article_id"
SOURCE_URL_META = "_coincourier_source_url"
MEDIA_PUBLICATION_KEY_META = "_coincourier_media_publication_key"


def _safe_prefix(conn: Any) -> str:
    prefix = get_wp_prefix(conn)
    if not re.fullmatch(r"[A-Za-z0-9_]+", prefix):
        raise ValueError("unsafe WordPress table prefix")
    return prefix


def find_post_by_id(conn: Any, post_id: int) -> dict[str, Any] | None:
    prefix = _safe_prefix(conn)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT p.ID, p.guid, pm.meta_value AS publication_key
            FROM `{prefix}posts` p
            LEFT JOIN `{prefix}postmeta` pm
              ON pm.post_id = p.ID AND pm.meta_key = %s
            WHERE p.ID = %s
              AND p.post_type = 'post'
              AND p.post_status <> 'trash'
            ORDER BY pm.meta_id ASC
            LIMIT 1
            """,
            (PUBLICATION_KEY_META, post_id),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


def find_post_by_publication_key(conn: Any, publication_key: str) -> dict[str, Any] | None:
    return _find_by_meta(conn, PUBLICATION_KEY_META, publication_key, "post")


def find_media_by_publication_key(conn: Any, publication_key: str) -> dict[str, Any] | None:
    return _find_by_meta(conn, MEDIA_PUBLICATION_KEY_META, publication_key, "attachment")


def media_exists(conn: Any, media_id: int) -> bool:
    prefix = _safe_prefix(conn)
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT 1 FROM `{prefix}posts` "
            "WHERE ID = %s AND post_type = 'attachment' AND post_status <> 'trash' LIMIT 1",
            (media_id,),
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def write_publication_metadata(
    conn: Any,
    post_id: int,
    metadata: Mapping[str, Any],
) -> None:
    prefix = _safe_prefix(conn)
    for key in (
        PUBLICATION_KEY_META,
        RAW_ARTICLE_ID_META,
        RICH_ARTICLE_ID_META,
        SOURCE_URL_META,
    ):
        value = metadata.get(key)
        if value not in (None, ""):
            _set_postmeta(conn, prefix, post_id, key, str(value))


def write_media_publication_key(conn: Any, media_id: int, publication_key: str) -> None:
    prefix = _safe_prefix(conn)
    _set_postmeta(conn, prefix, media_id, MEDIA_PUBLICATION_KEY_META, publication_key)


def _set_postmeta(
    conn: Any,
    prefix: str,
    post_id: int,
    key: str,
    value: str,
) -> None:
    """Update one existing meta row or insert it when absent."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT meta_id FROM `{prefix}postmeta` "
            "WHERE post_id = %s AND meta_key = %s ORDER BY meta_id ASC LIMIT 1 FOR UPDATE",
            (post_id, key),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                f"UPDATE `{prefix}postmeta` SET meta_value = %s WHERE meta_id = %s",
                (value, existing[0]),
            )
        else:
            cursor.execute(
                f"INSERT INTO `{prefix}postmeta` (post_id, meta_key, meta_value) "
                "VALUES (%s, %s, %s)",
                (post_id, key, value),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        cursor.close()


def _find_by_meta(
    conn: Any,
    meta_key: str,
    meta_value: str,
    post_type: str,
) -> dict[str, Any] | None:
    prefix = _safe_prefix(conn)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT p.ID, p.guid
            FROM `{prefix}posts` p
            JOIN `{prefix}postmeta` pm ON pm.post_id = p.ID
            WHERE pm.meta_key = %s
              AND pm.meta_value = %s
              AND p.post_type = %s
              AND p.post_status <> 'trash'
            ORDER BY p.ID ASC
            LIMIT 1
            """,
            (meta_key, meta_value, post_type),
        )
        return cursor.fetchone()
    finally:
        cursor.close()
