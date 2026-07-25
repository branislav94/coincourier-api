from __future__ import annotations

from abc import ABC, abstractmethod
import re

from .license_policy import ImageLicensePolicy
from .models import ImageCandidate


SAFE_FAILURE_REASONS = {
    "connection_failure",
    "http_error",
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
    ) -> None:
        self.provider = re.sub(r"[^a-z0-9_-]", "", (provider or "provider").lower()) or "provider"
        self.reason = reason if reason in SAFE_FAILURE_REASONS else "request_error"
        self.status_code = int(status_code) if status_code is not None else None
        self.retry_exhausted = bool(retry_exhausted)
        parts = [f"provider={self.provider}", f"reason={self.reason}"]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.retry_exhausted:
            parts.append("retry_exhausted=true")
        super().__init__(" ".join(parts))


class ImageSearchProvider(ABC):
    provider_name: str
    enabled: bool
    candidate_threshold: float

    @abstractmethod
    def search(self, query: str) -> list[ImageCandidate]:
        """Return normalized candidates for one query."""

    @abstractmethod
    def normalize_candidate(self, raw: dict, query: str, provider_rank: int) -> ImageCandidate | None:
        """Normalize one provider response item."""

    def validate_license(self, candidate: ImageCandidate, policy: ImageLicensePolicy) -> tuple[bool, str]:
        return policy.validate(candidate)
