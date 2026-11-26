"""Strict, connection-free editorial mode parsing and non-secret summaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DUPLICATE_MODE_FIELD = "DUPLICATE_ENFORCEMENT_MODE"
PUBLIC_COPY_MODE_FIELD = "PUBLIC_COPY_GATE_MODE"
RAG_CONTEXT_MODE_FIELD = 'RAG_CONTEXT_MODE'


class EditorialModeError(ValueError):
    """An error containing only a controlled field name and explanation."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


@dataclass(frozen=True)
class EditorialMode:
    mode: str
    source: str


def _read_mode(
    environment: Mapping[str, str], field: str, allowed: tuple[str, ...], *, default: str = 'shadow'
) -> EditorialMode:
    supplied = field in environment
    raw = environment.get(field, default)
    mode = raw.strip().lower() if isinstance(raw, str) else ""
    if mode not in allowed:
        raise EditorialModeError(field, "must be " + ", ".join(allowed[:-1]) + " or " + allowed[-1])
    return EditorialMode(mode, "environment" if supplied else "default")


def duplicate_mode(environment: Mapping[str, str]) -> EditorialMode:
    # This misspelling was observed in production. Never silently treat it as
    # an alias (which could accidentally activate enforcement), even if blank
    # or accompanied by the correctly spelled variable.
    if "DUPLICATE_ENFORCE_MODE" in environment:
        raise EditorialModeError(
            "DUPLICATE_ENFORCE_MODE",
            "is unsupported; use DUPLICATE_ENFORCEMENT_MODE (off, shadow or enforce)",
        )
    return _read_mode(environment, DUPLICATE_MODE_FIELD, ("off", "shadow", "enforce"))


def public_copy_mode(environment: Mapping[str, str]) -> EditorialMode:
    # G1A implements observation only. There is deliberately no enforce option.
    return _read_mode(environment, PUBLIC_COPY_MODE_FIELD, ("off", "shadow"))


def rag_context_mode(environment: Mapping[str, str]) -> EditorialMode:
    '''Read the historical context rollout mode without accessing external services.'''
    return _read_mode(environment, RAG_CONTEXT_MODE_FIELD, ('off', 'shadow', 'apply'), default='off')


def editorial_mode_summary(environment: Mapping[str, str]) -> dict[str, str]:
    duplicate = duplicate_mode(environment)
    public_copy = public_copy_mode(environment)
    rag_context = rag_context_mode(environment)
    return {
        "duplicate_enforcement_mode": duplicate.mode,
        "duplicate_mode_source": duplicate.source,
        "public_copy_gate_mode": public_copy.mode,
        "public_copy_mode_source": public_copy.source,
        'rag_context_mode': rag_context.mode,
        'rag_context_mode_source': rag_context.source,
    }
