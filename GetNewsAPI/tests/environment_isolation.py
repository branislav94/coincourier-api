"""Test-only protection from local dotenv credentials and external endpoints."""

from __future__ import annotations

import os
from typing import Mapping

import dotenv


SAFE_TEST_ENVIRONMENT = {
    "APP_ENV": "test",
    "DB_USER": "unit_test",
    "DB_PASSWORD": "unit_test_only",
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "1",
    "DB_NAME": "coincourier_unit_test",
    "DB_SSL_ENABLED": "false",
    "WP_API_URL": "https://wordpress.example.invalid",
    "WP_USERNAME": "unit_test",
    "WP_APP_PASSWORD": "unit_test_only",
    "WP_HTTP_CONNECT_TIMEOUT_SECONDS": "10",
    "WP_HTTP_READ_TIMEOUT_SECONDS": "60",
    "WP_DB_USER": "unit_test",
    "WP_DB_PASSWORD": "unit_test_only",
    "WP_DB_HOST": "127.0.0.1",
    "WP_DB_PORT": "1",
    "WP_DB_NAME": "wordpress_unit_test",
    "WP_DB_SSL_ENABLED": "false",
    "VECTOR_DB_USER": "unit_test",
    "VECTOR_DB_PASSWORD": "unit_test_only",
    "VECTOR_DB_HOST": "127.0.0.1",
    "VECTOR_DB_PORT": "1",
    "VECTOR_DB_NAME": "coincourier_vectors_unit_test",
    "VECTOR_DB_SSL_ENABLED": "false",
    "CRYPTO_NEWS_TOKEN": "unit-test-only",
    "GOOGLE_API_KEY": "unit-test-only",
    "GROK_API_KEY": "unit-test-only",
    "OPENAI_API_KEY": "unit-test-only",
    "GROK_BASE_URL": "https://api.example.invalid/v1",
    "OPENAI_BASE_URL": "https://api.example.invalid/v1",
    "ENABLE_APSCHEDULER": "false",
    "PROCESS_DURABLE_CLAIMS_ENABLED": "false",
    "PUBLISH_DURABLE_STATE_ENABLED": "false",
    "DUPLICATE_SHADOW_ENABLED": "false",
    "VECTOR_ENABLED": "false",
    "EMBEDDING_ENABLED": "false",
    "EMBEDDING_FRESH_START_AFTER_UTC": "",
    "SEMANTIC_SHADOW_ENABLED": "false",
    "IMAGE_SEARCH_ENGINE": "v1",
    "FILE_LOGGING_ENABLED": "false",
    "RUN_GROK_TEXT_SMOKE": "false",
    "RUN_GROK_IMAGE_SMOKE": "false",
    "GETNEWSAPI_RUNTIME_ENV_FILE": ".env.example",
}

SUBPROCESS_PASSTHROUGH_VARIABLES = {
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "PYTHONPATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


def _disabled_test_dotenv(*_args, **_kwargs) -> bool:
    return False


def install_test_environment() -> None:
    """Override connectivity settings and prevent dotenv reads in this test process."""

    os.environ.update(SAFE_TEST_ENVIRONMENT)
    dotenv.load_dotenv = _disabled_test_dotenv


def isolated_subprocess_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an explicit safe child environment, with deliberate overrides last."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in SUBPROCESS_PASSTHROUGH_VARIABLES
    }
    environment.update(SAFE_TEST_ENVIRONMENT)
    if overrides:
        environment.update(overrides)
    return environment
