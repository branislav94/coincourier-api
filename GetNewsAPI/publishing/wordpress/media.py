"""WordPress media transport and metadata requests."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from typing import Any

from config import WP_API_URL

from .client import API_BASE, session


def upload_media(
    content: bytes,
    filename: str | None,
    *,
    post_with_retries: Callable[..., Any],
    api_base: str | None = None,
) -> int:
    mime, _ = mimetypes.guess_type(filename or "image.jpg")
    headers = {
        "Content-Disposition": f"attachment; filename={filename or 'image.jpg'}",
        "Content-Type": mime or "image/jpeg",
    }
    response = post_with_retries(
        f"{API_BASE if api_base is None else api_base}/wp-json/wp/v2/media",
        headers=headers,
        data=content,
    )
    return response.json()["id"]


def set_media_details(
    media_id: int,
    alt_text: str,
    *,
    caption: str | None = None,
    description: str | None = None,
    http_session=None,
    api_url: str | None = None,
) -> None:
    payload: dict[str, str] = {"alt_text": (alt_text or "")[:120]}
    if caption:
        payload["caption"] = caption[:500]
    if description:
        payload["description"] = description[:1000]
    try:
        active_session = session if http_session is None else http_session
        active_session.post(
            f"{WP_API_URL if api_url is None else api_url}/wp-json/wp/v2/media/{media_id}",
            json=payload,
        ).raise_for_status()
    except Exception as exc:
        print(f"Could not set media details for media {media_id}: {exc}")


def set_media_alt(
    media_id: int,
    alt_text: str,
    *,
    http_session=None,
    api_url: str | None = None,
) -> None:
    try:
        active_session = session if http_session is None else http_session
        active_session.post(
            f"{WP_API_URL if api_url is None else api_url}/wp-json/wp/v2/media/{media_id}",
            json={"alt_text": (alt_text or "")[:120]},
        ).raise_for_status()
    except Exception as exc:
        print(f"⚠️  Could not set alt text for media {media_id}: {exc}")
