"""WordPress media transport and metadata requests."""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Callable
from typing import Any

from config import WP_API_URL

from .client import API_BASE, WP_HTTP_TIMEOUT, session


logger = logging.getLogger(__name__)
MAX_REQUIRED_ATTRIBUTION_CHARS = 16_384


class MediaMetadataError(RuntimeError):
    """A sanitized failure to preserve required image attribution."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_required_attribution(value: str | None) -> None:
    """Reject incomplete or unbounded credits instead of clipping required text."""
    if not isinstance(value, str) or not value.strip():
        raise MediaMetadataError("attribution_missing")
    if len(value) > MAX_REQUIRED_ATTRIBUTION_CHARS:
        raise MediaMetadataError("attribution_too_large")


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
        timeout=WP_HTTP_TIMEOUT,
    )
    return response.json()["id"]


def set_media_details(
    media_id: int,
    alt_text: str,
    *,
    caption: str | None = None,
    description: str | None = None,
    required_attribution: bool = False,
    http_session=None,
    api_url: str | None = None,
) -> bool:
    """Preserve complete stock credits, failing closed when they are required."""
    try:
        if required_attribution:
            validate_required_attribution(caption)
            validate_required_attribution(description)
        payload: dict[str, str] = {"alt_text": (alt_text or "")[:120]}
        if caption:
            payload["caption"] = caption if required_attribution else caption[:500]
        if description:
            payload["description"] = description if required_attribution else description[:1000]
        active_session = session if http_session is None else http_session
        active_session.post(
            f"{WP_API_URL if api_url is None else api_url}/wp-json/wp/v2/media/{media_id}",
            json=payload,
            timeout=WP_HTTP_TIMEOUT,
        ).raise_for_status()
        return True
    except Exception as exc:
        reason = exc.reason if isinstance(exc, MediaMetadataError) else "metadata_update_failed"
        logger.warning(
            "[IMG] media metadata failed media_id=%s reason=%s error_type=%s",
            media_id,
            reason,
            type(exc).__name__,
        )
        if required_attribution:
            raise MediaMetadataError(reason) from None
        return False


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
            timeout=WP_HTTP_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        print(
            f"Could not set alt text for media {media_id}: "
            f"{type(exc).__name__}"
        )
