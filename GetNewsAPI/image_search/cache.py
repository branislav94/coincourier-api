from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from config import (
    IMAGE_PROVIDER_MAX_RETRIES,
    IMAGE_PROVIDER_TIMEOUT_SECONDS,
    STOCK_IMAGE_CACHE_HOURS,
)

from .provider import ProviderUnavailable


logger = logging.getLogger(__name__)
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
SECRET_PARAM_NAMES = {"client_id", "client_secret", "key", "token"}


class CachedHttpClient:
    def __init__(
        self,
        *,
        session: Any | None = None,
        cache_root: str | Path | None = None,
        cache_hours: int = STOCK_IMAGE_CACHE_HOURS,
        timeout: int = IMAGE_PROVIDER_TIMEOUT_SECONDS,
        max_retries: int = IMAGE_PROVIDER_MAX_RETRIES,
        sleeper=time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.cache_root = Path(
            cache_root or os.getenv("STOCK_IMAGE_CACHE_DIR", "/app/cache/stock_images")
        ) / "v2"
        self.cache_hours = cache_hours
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.sleeper = sleeper

    def _cache_path(self, provider: str, query: str, params: dict[str, Any]) -> Path:
        public_params = {
            key: value
            for key, value in sorted(params.items())
            if key.lower() not in SECRET_PARAM_NAMES
        }
        seed = json.dumps(
            {"provider": provider, "query": query, "params": public_params},
            sort_keys=True,
            separators=(",", ":"),
        )
        key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return self.cache_root / provider / f"{key}.json"

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        if self.cache_hours <= 0:
            return None
        try:
            if not path.exists():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(payload.get("cached_at", 0)) > self.cache_hours * 3600:
                return None
            response = payload.get("response")
            return response if isinstance(response, dict) else None
        except Exception:
            logger.warning("[IMG-V2] response cache read failed path=%s", path, exc_info=True)
            return None

    def _write_cache(self, path: Path, data: dict[str, Any]) -> None:
        if self.cache_hours <= 0:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"cached_at": time.time(), "response": data}, ensure_ascii=True),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("[IMG-V2] response cache write failed path=%s", path, exc_info=True)

    def _request_json(self, method: str, provider: str, url: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                status = int(response.status_code)
                if 200 <= status < 300:
                    try:
                        data = response.json()
                    except Exception:
                        raise ProviderUnavailable(provider, "invalid_response") from None
                    if not isinstance(data, dict):
                        raise ProviderUnavailable(provider, "invalid_response")
                    return data
                if status not in RETRYABLE_STATUSES:
                    # Search API 401/403 means that provider access is unavailable,
                    # unlike a candidate image URL that may simply forbid hotlinking.
                    raise ProviderUnavailable(provider, "http_error", status_code=status)
                if attempt >= self.max_retries:
                    raise ProviderUnavailable(
                        provider,
                        "http_error",
                        status_code=status,
                        retry_exhausted=True,
                    )
                retry_after = response.headers.get("Retry-After") if response.headers else None
                try:
                    delay = min(5.0, max(0.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = min(2.0, 0.5 * (2**attempt))
                self.sleeper(delay)
            except ProviderUnavailable:
                raise
            except requests.Timeout:
                if attempt >= self.max_retries:
                    raise ProviderUnavailable(
                        provider,
                        "timeout",
                        retry_exhausted=True,
                    ) from None
                self.sleeper(min(2.0, 0.5 * (2**attempt)))
            except requests.ConnectionError:
                if attempt >= self.max_retries:
                    raise ProviderUnavailable(
                        provider,
                        "connection_failure",
                        retry_exhausted=True,
                    ) from None
                self.sleeper(min(2.0, 0.5 * (2**attempt)))
            except Exception:
                raise ProviderUnavailable(provider, "request_error") from None

        raise ProviderUnavailable(provider, "request_error", retry_exhausted=True)

    def get_json(
        self,
        provider: str,
        query: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        path = self._cache_path(provider, query, params)
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        data = self._request_json("GET", provider, url, headers=headers, params=params)
        self._write_cache(path, data)
        return data

    def post_json(
        self,
        provider: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json("POST", provider, url, data=data or {})
