"""Offline deployment configuration validation.

This module deliberately reads values only from a supplied mapping or the process
environment. It never opens a socket, creates a database connection, or renders
secret values in validation output.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "n", "off"})
CORE_DB_FIELDS = ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_NAME")
VECTOR_DB_FIELDS = (
    "VECTOR_DB_USER",
    "VECTOR_DB_PASSWORD",
    "VECTOR_DB_HOST",
    "VECTOR_DB_NAME",
)
WORDPRESS_REST_FIELDS = ("WP_API_URL", "WP_USERNAME", "WP_APP_PASSWORD")
WORDPRESS_DB_FIELDS = (
    "WP_DB_USER",
    "WP_DB_PASSWORD",
    "WP_DB_HOST",
    "WP_DB_NAME",
)
PROFILES = frozenset({"web", "pipeline", "publish", "embedding"})
@dataclass(frozen=True)
class ConfigIssue:
    field: str
    message: str

    def render(self) -> str:
        return f"{self.field}: {self.message}"


def _value(environment: Mapping[str, str], name: str) -> str:
    return (environment.get(name) or "").strip()


def _require(
    environment: Mapping[str, str],
    names: tuple[str, ...],
    issues: list[ConfigIssue],
) -> None:
    for name in names:
        if not _value(environment, name):
            issues.append(ConfigIssue(name, "is required"))


def _boolean(
    environment: Mapping[str, str],
    name: str,
    default: bool,
    issues: list[ConfigIssue],
) -> bool:
    raw = _value(environment, name)
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    issues.append(ConfigIssue(name, "must be true or false"))
    return default


def _positive_integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    issues: list[ConfigIssue],
    *,
    maximum: int | None = None,
) -> int:
    raw = _value(environment, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        issues.append(ConfigIssue(name, "must be a positive integer"))
        return default
    if value <= 0 or (maximum is not None and value > maximum):
        issues.append(ConfigIssue(name, "must be a positive integer in range"))
        return default
    return value


def _validate_tls(
    environment: Mapping[str, str],
    prefix: str,
    issues: list[ConfigIssue],
    *,
    default_enabled: bool,
) -> None:
    enabled = _boolean(
        environment,
        f"{prefix}_SSL_ENABLED",
        default_enabled,
        issues,
    )
    verify_cert = _boolean(
        environment,
        f"{prefix}_SSL_VERIFY_CERT",
        False,
        issues,
    )
    verify_identity = _boolean(
        environment,
        f"{prefix}_SSL_VERIFY_IDENTITY",
        False,
        issues,
    )
    if verify_cert and not enabled:
        issues.append(
            ConfigIssue(
                f"{prefix}_SSL_VERIFY_CERT",
                f"requires {prefix}_SSL_ENABLED=true",
            )
        )
    if verify_identity and not verify_cert:
        issues.append(
            ConfigIssue(
                f"{prefix}_SSL_VERIFY_IDENTITY",
                f"requires {prefix}_SSL_VERIFY_CERT=true",
            )
        )
    if verify_cert and not _value(environment, f"{prefix}_SSL_CA"):
        issues.append(
            ConfigIssue(
                f"{prefix}_SSL_CA",
                "is required when certificate verification is enabled",
            )
        )


def _validate_provider_keys(
    environment: Mapping[str, str],
    issues: list[ConfigIssue],
) -> None:
    providers = {
        _value(environment, "PRIMARY_LLM_PROVIDER") or "grok",
        _value(environment, "LLM_FALLBACK_PROVIDER") or "openai",
    }
    if "grok" in providers and not (
        _value(environment, "GROK_API_KEY")
        or _value(environment, "XAI_API_KEY")
    ):
        issues.append(ConfigIssue("GROK_API_KEY", "is required for Grok routing"))
    if "openai" in providers and not _value(environment, "OPENAI_API_KEY"):
        issues.append(ConfigIssue("OPENAI_API_KEY", "is required for OpenAI routing"))


def validate_runtime_config(
    environment: Mapping[str, str] | None = None,
    *,
    profile: str = "web",
) -> tuple[ConfigIssue, ...]:
    """Return safe, deterministic issues for one runtime profile."""

    env = os.environ if environment is None else environment
    normalized_profile = profile.strip().lower()
    issues: list[ConfigIssue] = []

    if normalized_profile not in PROFILES:
        return (ConfigIssue("profile", "must be web, pipeline, publish, or embedding"),)

    app_env = (_value(env, "APP_ENV") or "development").lower()
    if app_env not in {"development", "test", "production"}:
        issues.append(
            ConfigIssue("APP_ENV", "must be development, test, or production")
        )

    _require(env, CORE_DB_FIELDS, issues)
    _positive_integer(env, "DB_PORT", 3306, issues, maximum=65535)
    _positive_integer(env, "DB_CONNECT_TIMEOUT_SECONDS", 5, issues)
    _positive_integer(env, "READINESS_DB_TIMEOUT_SECONDS", 3, issues)
    _validate_tls(env, "DB", issues, default_enabled=True)

    if normalized_profile == "web":
        _require(env, ("PUBLISH_API_TOKEN",), issues)

    vector_enabled = _boolean(env, "VECTOR_ENABLED", False, issues)
    embedding_enabled = _boolean(env, "EMBEDDING_ENABLED", False, issues)
    semantic_enabled = _boolean(env, "SEMANTIC_SHADOW_ENABLED", False, issues)

    scheduler_enabled = _boolean(env, "ENABLE_APSCHEDULER", False, issues)
    for flag in (
        "PROCESS_DURABLE_CLAIMS_ENABLED",
        "PUBLISH_DURABLE_STATE_ENABLED",
        "DUPLICATE_SHADOW_ENABLED",
        "FILE_LOGGING_ENABLED",
    ):
        _boolean(env, flag, False, issues)

    if vector_enabled:
        _require(env, VECTOR_DB_FIELDS, issues)
        _positive_integer(env, "VECTOR_DB_PORT", 3306, issues, maximum=65535)
        _positive_integer(env, "VECTOR_DB_CONNECT_TIMEOUT_SECONDS", 5, issues)
        _validate_tls(env, "VECTOR_DB", issues, default_enabled=True)

    if embedding_enabled or normalized_profile == "embedding":
        if not vector_enabled:
            issues.append(
                ConfigIssue("VECTOR_ENABLED", "must be true when embeddings are enabled")
            )
        if (_value(env, "EMBEDDING_PROVIDER") or "openai").lower() != "openai":
            issues.append(ConfigIssue("EMBEDDING_PROVIDER", "must be openai"))
        if not _value(env, "OPENAI_API_KEY"):
            issues.append(
                ConfigIssue("OPENAI_API_KEY", "is required for OpenAI embeddings")
            )
        if not _value(env, "EMBEDDING_MODEL"):
            issues.append(ConfigIssue("EMBEDDING_MODEL", "is required"))
        if not _value(env, "EMBEDDING_CHUNKER_VERSION"):
            issues.append(ConfigIssue("EMBEDDING_CHUNKER_VERSION", "is required"))
        dimensions = _positive_integer(env, "EMBEDDING_DIMENSIONS", 1536, issues)
        if dimensions != 1536:
            issues.append(
                ConfigIssue("EMBEDDING_DIMENSIONS", "must match the approved 1536 schema")
            )

    if semantic_enabled and not vector_enabled:
        issues.append(
            ConfigIssue(
                "SEMANTIC_SHADOW_ENABLED",
                "requires VECTOR_ENABLED=true",
            )
        )

    if normalized_profile == "pipeline" or scheduler_enabled:
        _require(env, ("CRYPTO_NEWS_TOKEN", "GOOGLE_API_KEY"), issues)
        _validate_provider_keys(env, issues)

    if normalized_profile == "publish" or scheduler_enabled:
        _require(env, WORDPRESS_REST_FIELDS + WORDPRESS_DB_FIELDS, issues)
        _positive_integer(env, "WP_DB_PORT", 3306, issues, maximum=65535)
        _positive_integer(env, "WP_DB_CONNECT_TIMEOUT_SECONDS", 5, issues)
        _validate_tls(env, "WP_DB", issues, default_enabled=True)

    return tuple(issues)


def format_config_issues(issues: tuple[ConfigIssue, ...]) -> str:
    """Render issues without including environment values."""

    return "\n".join(f"- {issue.render()}" for issue in issues)
