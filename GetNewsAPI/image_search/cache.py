from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import requests

from config import (
    IMAGE_PROVIDER_MAX_RETRIES,
    IMAGE_PROVIDER_TIMEOUT_SECONDS,
    STOCK_IMAGE_CACHE_DIR,
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
        self.cache_root = Path(cache_root or STOCK_IMAGE_CACHE_DIR) / "v2"
        self.cache_hours = cache_hours
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.sleeper = sleeper

    def _cache_path(self, provider: str, query: str, params: dict[str, Any], url: str = "") -> Path:
        public_params = {
            key: value
            for key, value in sorted(params.items())
            if key.lower() not in SECRET_PARAM_NAMES
        }
        cache_identity = {"provider": provider, "query": query, "params": public_params}
        if provider == "openverse":
            cache_identity["endpoint"] = url
        seed = json.dumps(
            cache_identity,
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

    @staticmethod
    def _retry_after_delay(headers: Any, fallback: float) -> float:
        """Honor numeric or HTTP-date server delays without retaining raw headers."""
        value = headers.get("Retry-After") if headers else None
        if value is None:
            return fallback
        try:
            delay = float(value)
        except (TypeError, ValueError, OverflowError):
            try:
                when = parsedate_to_datetime(str(value))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = max(0.0, when.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return fallback
        return delay if math.isfinite(delay) and delay >= 0 else fallback

    @staticmethod
    def _payload_retry_delay(
        data: dict[str, Any],
        policy: Callable[[dict[str, Any]], float | None] | None,
    ) -> float | None:
        if policy is None:
            return None
        try:
            delay = policy(data)
            if delay is None:
                return None
            delay = float(delay)
            return delay if math.isfinite(delay) and delay >= 0 else None
        except Exception:
            # Policy exceptions may contain response bodies or request secrets.
            return None

    def _request_json(
        self,
        method: str,
        provider: str,
        url: str,
        *,
        payload_validator: Callable[[dict[str, Any]], bool] | None = None,
        payload_retry_delay: Callable[[dict[str, Any]], float | None] | None = None,
        classify_edge_denials: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                status = int(response.status_code)
                diagnostics = self._safe_response_diagnostics(response) if classify_edge_denials else {}
                if status == 403 and diagnostics.get("content_type") == "text/html":
                    raise ProviderUnavailable(provider, "edge_access_denied", status_code=status, **diagnostics)
                if 200 <= status < 300:
                    try:
                        data = response.json()
                    except Exception:
                        raise ProviderUnavailable(provider, "invalid_response") from None
                    if not isinstance(data, dict):
                        raise ProviderUnavailable(provider, "invalid_response")
                    if not self._valid_payload(data, payload_validator):
                        # A provider may explicitly classify a rejected payload as
                        # temporary. Ordinary malformed responses remain terminal.
                        delay = self._payload_retry_delay(data, payload_retry_delay)
                        if delay is None:
                            raise ProviderUnavailable(provider, "invalid_response")
                        if attempt >= self.max_retries:
                            raise ProviderUnavailable(provider, "invalid_response", retry_exhausted=True)
                        self.sleeper(self._retry_after_delay(response.headers, delay))
                        continue
                    return data
                if status not in RETRYABLE_STATUSES:
                    # Search API 401/403 means that provider access is unavailable,
                    # unlike a candidate image URL that may simply forbid hotlinking.
                    raise ProviderUnavailable(provider, "http_error", status_code=status, **diagnostics)
                if attempt >= self.max_retries:
                    raise ProviderUnavailable(
                        provider,
                        "http_error",
                        status_code=status,
                        retry_exhausted=True,
                        **diagnostics,
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
        payload_validator: Callable[[dict[str, Any]], bool] | None = None,
        payload_retry_delay: Callable[[dict[str, Any]], float | None] | None = None,
        proxies: dict[str, str] | None = None,
        classify_edge_denials: bool = False,
        allow_redirects: bool | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        path = self._cache_path(provider, query, params, url)
        cached = self._read_cache(path)
        if cached is not None and self._valid_payload(cached, payload_validator):
            return cached
        data = self._request_json(
            "GET", provider, url, headers=headers, params=params,
            payload_validator=payload_validator, payload_retry_delay=payload_retry_delay,
            classify_edge_denials=classify_edge_denials,
            **({"proxies": dict(proxies)} if proxies is not None else {}),
            **({"allow_redirects": allow_redirects} if allow_redirects is not None else {}),
        )
        self._write_cache(path, data)
        return data

    @staticmethod
    def _safe_response_diagnostics(response: Any) -> dict[str, Any]:
        headers = response.headers or {}
        mime = str(headers.get("Content-Type") or "").partition(";")[0].strip().lower()
        content_type = mime if mime in {"text/html", "application/json"} else "other"
        return {
            "content_type": content_type,
            "server_cloudflare": str(headers.get("Server") or "").strip().lower() == "cloudflare",
            "cf_ray_present": bool(headers.get("CF-Ray")),
        }

    @staticmethod
    def _valid_payload(
        data: dict[str, Any],
        validator: Callable[[dict[str, Any]], bool] | None,
    ) -> bool:
        if validator is None:
            return True
        try:
            return bool(validator(data))
        except Exception:
            # Provider response bodies and validator exceptions may contain secrets.
            return False

    def post_json(
        self,
        provider: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        proxies: dict[str, str] | None = None,
        classify_edge_denials: bool = False,
        allow_redirects: bool | None = None,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST", provider, url, data=data or {},
            classify_edge_denials=classify_edge_denials,
            **({"proxies": dict(proxies)} if proxies is not None else {}),
            **({"allow_redirects": allow_redirects} if allow_redirects is not None else {}),
        )
