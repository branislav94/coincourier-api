"""WordPress authentication and HTTP retry behavior."""

from __future__ import annotations

import base64
import time
from collections.abc import Collection
from typing import Any

import requests

from config import (
    WP_API_URL,
    WP_APP_PASSWORD,
    WP_HTTP_CONNECT_TIMEOUT_SECONDS,
    WP_HTTP_READ_TIMEOUT_SECONDS,
    WP_USERNAME,
)


API_BASE = WP_API_URL.rstrip("/")
RETRY_STATUS = {429, 500, 502, 503, 504}
WP_HTTP_TIMEOUT = (
    WP_HTTP_CONNECT_TIMEOUT_SECONDS,
    WP_HTTP_READ_TIMEOUT_SECONDS,
)


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
    """POST with bounded I/O and retries only for known-safe failure classes."""
    statuses = RETRY_STATUS if retry_status is None else retry_status
    kwargs.setdefault("timeout", WP_HTTP_TIMEOUT)
    for attempt in range(1, max_tries + 1):
        try:
            response = http_session.post(url, **kwargs)
            if response.status_code in statuses:
                if attempt < max_tries:
                    print(
                        f"WP POST retry {attempt}/{max_tries} after {pause_s}s "
                        f"for HTTP {response.status_code}"
                    )
                    time.sleep(pause_s)
                    continue
            response.raise_for_status()
            return response
        except requests.ConnectTimeout as exc:
            if attempt < max_tries:
                print(
                    f"WP POST retry {attempt}/{max_tries} after {pause_s}s "
                    f"after {type(exc).__name__}"
                )
                time.sleep(pause_s)
            else:
                print(
                    f"WP POST failed after {max_tries} tries: "
                    f"{type(exc).__name__}"
                )
                raise
        except requests.RequestException as exc:
            # The request may have reached WordPress. Reconcile on the next run.
            print(f"WP POST failed without retry: {type(exc).__name__}")
            raise

    return None
