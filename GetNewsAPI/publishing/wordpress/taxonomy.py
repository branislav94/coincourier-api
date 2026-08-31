"""WordPress category and tag resolution."""

from __future__ import annotations

import re

from config import WP_API_URL

from .client import API_BASE, session


def slugify(text: str) -> str:
    text = text.lower().strip()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def ensure_category(
    name: str,
    *,
    http_session=None,
    api_url: str | None = None,
) -> int:
    slug = slugify(name)
    active_session = session if http_session is None else http_session
    active_api_url = WP_API_URL if api_url is None else api_url
    response = active_session.get(
        f"{active_api_url}/wp-json/wp/v2/categories",
        params={"slug": slug},
    )
    response.raise_for_status()
    if response.json():
        return response.json()[0]["id"]

    response = active_session.post(
        f"{active_api_url}/wp-json/wp/v2/categories",
        json={"name": name, "slug": slug},
    )
    response.raise_for_status()
    return response.json()["id"]


def ensure_term(
    name: str,
    taxonomy: str,
    *,
    http_session=None,
    api_base: str | None = None,
) -> int:
    slug = slugify(name)
    active_session = session if http_session is None else http_session
    active_api_base = API_BASE if api_base is None else api_base
    response = active_session.get(
        f"{active_api_base}/wp-json/wp/v2/{taxonomy}",
        params={"slug": slug},
    )
    response.raise_for_status()
    if response.json():
        return response.json()[0]["id"]
    response = active_session.post(
        f"{active_api_base}/wp-json/wp/v2/{taxonomy}",
        json={"name": name, "slug": slug},
    )
    response.raise_for_status()
    return response.json()["id"]
