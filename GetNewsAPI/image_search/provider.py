from __future__ import annotations

from abc import ABC, abstractmethod
import re

from .license_policy import ImageLicensePolicy
from .models import ImageCandidate


SAFE_FAILURE_REASONS = {
    "authentication_failed",
    "connection_failure",
    "edge_access_denied",
    "http_error",
    "invalid_configuration",
    "invalid_response",
    "request_error",
    "timeout",
}


class ProviderUnavailable(RuntimeError):
    """A sanitized provider-level failure that never retains request details."""

    def __init__(
        self,
        provider: str,
        reason: str = "request_error",
        *,
        status_code: int | None = None,
        retry_exhausted: bool = False,
        content_type: str | None = None,
        server_cloudflare: bool = False,
        cf_ray_present: bool = False,
    ) -> None:
        self.provider = re.sub(r"[^a-z0-9_-]", "", (provider or "provider").lower()) or "provider"
        self.reason = reason if reason in SAFE_FAILURE_REASONS else "request_error"
        self.status_code = int(status_code) if status_code is not None else None
        self.retry_exhausted = bool(retry_exhausted)
        self.content_type = content_type if content_type in {"text/html", "application/json", "other"} else None
        self.server_cloudflare = bool(server_cloudflare)
        self.cf_ray_present = bool(cf_ray_present)
        parts = [f"provider={self.provider}", f"reason={self.reason}"]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.retry_exhausted:
            parts.append("retry_exhausted=true")
        if self.content_type is not None:
            parts.extend((
                f"content_type={self.content_type}",
                f"server_cloudflare={str(self.server_cloudflare).lower()}",
                f"cf_ray_present={str(self.cf_ray_present).lower()}",
            ))
        super().__init__(" ".join(parts))


class ImageSearchProvider(ABC):
    provider_name: str
    enabled: bool
    candidate_threshold: float

    @staticmethod
    def valid_result_list(payload: dict, field: str) -> bool:
        return (
            "error" not in payload
            and "errors" not in payload
            and isinstance(payload.get(field), list)
            and all(isinstance(item, dict) for item in payload[field])
        )

    def normalize_results(self, items: list[dict], query: str) -> list[ImageCandidate]:
        candidates = [
            candidate
            for rank, raw in enumerate(items)
            if (candidate := self.normalize_candidate(raw, query, rank)) is not None
        ]
        self.last_search_stats = {"raw": len(items), "normalized": len(candidates)}
        self.last_search_rejections = (
            {"required_fields": len(items) - len(candidates)} if len(items) > len(candidates) else {}
        )
        return candidates

    @abstractmethod
    def search(self, query: str) -> list[ImageCandidate]:
        """Return normalized candidates for one query."""

    @abstractmethod
    def normalize_candidate(self, raw: dict, query: str, provider_rank: int) -> ImageCandidate | None:
        """Normalize one provider response item."""

    def validate_license(self, candidate: ImageCandidate, policy: ImageLicensePolicy) -> tuple[bool, str]:
        return policy.validate(candidate)
