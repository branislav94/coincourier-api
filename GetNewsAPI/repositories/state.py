"""Shared primitives for short-lived durable claims."""

from __future__ import annotations

import secrets


def new_claim_token() -> str:
    """Return a cryptographically strong token suitable for durable ownership."""
    return secrets.token_hex(32)


def claim_prefix(token: str) -> str:
    """Return the only portion of a claim token that may be logged."""
    return token[:8]


def safe_error_message(error: BaseException, operation: str) -> str:
    """Build a bounded diagnostic without persisting request or article content."""
    error_type = type(error).__name__ or "Error"
    return f"{error_type}: {operation} failed"[:500]
