"""WordPress authentication and HTTP retry behavior."""

from __future__ import annotations

import base64
import time
from collections.abc import Collection
from typing import Any

import requests

from config import WP_API_URL, WP_APP_PASSWORD, WP_USERNAME


API_BASE = WP_API_URL.rstrip("/")
RETRY_STATUS = {429, 500, 502, 503, 504}


def create_authenticated_session(username: str, password: str) -> requests.Session:
    http_session = requests.Session()
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    http_session.headers.update({
        "Authorization": f"Basic {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    })
    return http_session


session = create_authenticated_session(WP_USERNAME, WP_APP_PASSWORD)


def post_with_retries(
    http_session: requests.Session,
    url: str,
    *,
    max_tries: int = 3,
    pause_s: int = 10,
    retry_status: Collection[int] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Preserve the existing WordPress POST retry semantics."""
    statuses = RETRY_STATUS if retry_status is None else retry_status
    for attempt in range(1, max_tries + 1):
        try:
            response = http_session.post(url, **kwargs)
            if response.status_code in statuses:
                raise requests.HTTPError(
                    f"{response.status_code} {response.reason}",
                    response=response,
                )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if attempt < max_tries:
                print(f"⚠️  WP POST retry {attempt}/{max_tries} after {pause_s}s: {exc}")
                time.sleep(pause_s)
            else:
                print(f"❌  WP POST failed after {max_tries} tries: {exc}")
                raise

    return None
