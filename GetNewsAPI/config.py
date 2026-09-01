"""
Configuration module.

Loads environment variables from a local .env file and exposes:
- API tokens/keys
- WordPress REST credentials
- Image generation settings
- MySQL connection dictionaries for the app DB, vector DB, and WordPress DB

All values are sourced from environment variables to avoid hardcoding secrets.
"""


import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value such as true or false")


def _parse_optional_utc(name: str) -> datetime | None:
    value = (os.getenv(name) or "").strip()
    if not value:
        return None

    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a UTC timestamp like '2026-06-27 18:00:00' "
            "or '2026-06-27T18:00:00Z'"
        ) from exc

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


ENABLE_APSCHEDULER = _env_bool("ENABLE_APSCHEDULER", False)
FLASK_DEBUG = _env_bool("FLASK_DEBUG", False)
PIPELINE_FRESH_START_AFTER_UTC = _parse_optional_utc("PIPELINE_FRESH_START_AFTER_UTC")
PIPELINE_FRESH_START_AFTER_UTC_SQL = (
    PIPELINE_FRESH_START_AFTER_UTC.strftime("%Y-%m-%d %H:%M:%S")
    if PIPELINE_FRESH_START_AFTER_UTC
    else None
)

# Crypto News API token
CRYPTO_NEWS_TOKEN = os.getenv("CRYPTO_NEWS_TOKEN")

# AI provider routing
PRIMARY_LLM_PROVIDER = os.getenv("PRIMARY_LLM_PROVIDER", "grok").strip().lower()
LLM_FALLBACK_PROVIDER = os.getenv("LLM_FALLBACK_PROVIDER", "openai").strip().lower()

GROK_API_KEY = os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY")
GROK_BASE_URL = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")
GROK_TEXT_MODEL = os.getenv("GROK_TEXT_MODEL", "grok-4.3")
GROK_REASONING_EFFORT = os.getenv("GROK_REASONING_EFFORT", "").strip().lower()
GROK_REASONING_EFFORT_DEFAULT = os.getenv("GROK_REASONING_EFFORT_DEFAULT", "low").strip().lower()
GROK_REASONING_EFFORT_REWRITE = os.getenv("GROK_REASONING_EFFORT_REWRITE", "").strip().lower()
GROK_REASONING_EFFORT_EXPANSION = os.getenv("GROK_REASONING_EFFORT_EXPANSION", "").strip().lower()
GROK_REASONING_EFFORT_REPAIR = os.getenv("GROK_REASONING_EFFORT_REPAIR", "").strip().lower()
GROK_REASONING_EFFORT_SCORING = os.getenv("GROK_REASONING_EFFORT_SCORING", "").strip().lower()
GROK_REASONING_EFFORT_CLASSIFY = os.getenv("GROK_REASONING_EFFORT_CLASSIFY", "").strip().lower()
GROK_REASONING_EFFORT_ANALYSIS = os.getenv("GROK_REASONING_EFFORT_ANALYSIS", "").strip().lower()
GROK_MAX_OUTPUT_TOKENS = int(os.getenv("GROK_MAX_OUTPUT_TOKENS", "4096"))

_GROK_REASONING_BY_PHASE = {
    "rewrite": GROK_REASONING_EFFORT_REWRITE,
    "expansion": GROK_REASONING_EFFORT_EXPANSION,
    "repair": GROK_REASONING_EFFORT_REPAIR,
    "scoring": GROK_REASONING_EFFORT_SCORING,
    "classify": GROK_REASONING_EFFORT_CLASSIFY,
    "analysis": GROK_REASONING_EFFORT_ANALYSIS,
}


def get_grok_reasoning_effort(phase: str | None = None) -> str:
    phase_key = (phase or "").strip().lower()
    candidates = (
        _GROK_REASONING_BY_PHASE.get(phase_key),
        GROK_REASONING_EFFORT_DEFAULT,
        GROK_REASONING_EFFORT,
        "low",
    )
    for value in candidates:
        effort = (value or "").strip().lower()
        if effort in {"low", "medium", "high"}:
            return effort
    return "low"

# OpenAI API Key and fallback settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-5")
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "minimal").strip().lower()
OPENAI_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "4096"))
SEO_PLUGIN = os.getenv("SEO_PLUGIN", "yoast").strip().lower()

USE_API_IMAGES = int(os.getenv("USE_API_IMAGES", "1"))
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", os.getenv("IMAGE_MODEL", "gpt-image-1"))
IMAGE_MODEL = OPENAI_IMAGE_MODEL
IMAGE_QUALITY = os.getenv("IMAGE_QUALITY", "high")
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1024x1024")
IMAGE_SOURCE_MODE = os.getenv("IMAGE_SOURCE_MODE", "hybrid").strip().lower()
IMAGE_SOURCE_PRIORITY = os.getenv("IMAGE_SOURCE_PRIORITY", "stock_first").strip().lower()
USE_SOURCE_IMAGES = _env_bool("USE_SOURCE_IMAGES", False)
PRIMARY_IMAGE_PROVIDER = os.getenv("PRIMARY_IMAGE_PROVIDER", "grok").strip().lower()
IMAGE_FALLBACK_PROVIDER = os.getenv("IMAGE_FALLBACK_PROVIDER", "openai").strip().lower()

GROK_IMAGE_MODEL = os.getenv("GROK_IMAGE_MODEL", "grok-imagine-image-quality")
GROK_IMAGE_ASPECT_RATIO = os.getenv("GROK_IMAGE_ASPECT_RATIO", "16:9")
GROK_IMAGE_RESOLUTION = os.getenv("GROK_IMAGE_RESOLUTION", "1k")

OPENAI_IMAGE_FALLBACK = _env_bool("OPENAI_IMAGE_FALLBACK", True)

IMAGE_SEARCH_ENGINE = os.getenv("IMAGE_SEARCH_ENGINE", "v1").strip().lower()
if IMAGE_SEARCH_ENGINE not in {"v1", "v2"}:
    raise ValueError("IMAGE_SEARCH_ENGINE must be v1 or v2")

IMAGE_SEARCH_PROVIDERS = tuple(
    provider.strip().lower()
    for provider in os.getenv(
        "IMAGE_SEARCH_PROVIDERS",
        "pexels,pixabay,openverse",
    ).split(",")
    if provider.strip()
)
IMAGE_SEARCH_RESULTS_PER_QUERY = int(os.getenv("IMAGE_SEARCH_RESULTS_PER_QUERY", "10"))
IMAGE_GLOBAL_CANDIDATE_LIMIT = int(os.getenv("IMAGE_GLOBAL_CANDIDATE_LIMIT", "50"))
IMAGE_MIN_WIDTH = int(os.getenv("IMAGE_MIN_WIDTH", "1200"))
IMAGE_MIN_HEIGHT = int(os.getenv("IMAGE_MIN_HEIGHT", "675"))
IMAGE_PROVIDER_TIMEOUT_SECONDS = int(os.getenv("IMAGE_PROVIDER_TIMEOUT_SECONDS", "10"))
IMAGE_PROVIDER_MAX_RETRIES = int(os.getenv("IMAGE_PROVIDER_MAX_RETRIES", "1"))
IMAGE_LICENSE_ALLOWLIST = tuple(
    license_name.strip().lower()
    for license_name in os.getenv("IMAGE_LICENSE_ALLOWLIST", "cc0,pdm,cc-by").split(",")
    if license_name.strip()
)
IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED = _env_bool(
    "IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED",
    True,
)
IMAGE_GENERATION_ON_PROVIDER_ERROR = _env_bool("IMAGE_GENERATION_ON_PROVIDER_ERROR", False)

PEXELS_ENABLED = _env_bool("PEXELS_ENABLED", True)
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PEXELS_ORIENTATION = os.getenv("PEXELS_ORIENTATION", "landscape")
PEXELS_PER_PAGE = int(os.getenv("PEXELS_PER_PAGE", "10"))
PEXELS_MIN_SCORE = float(os.getenv("PEXELS_MIN_SCORE", "0.72"))

PIXABAY_ENABLED = _env_bool("PIXABAY_ENABLED", True)
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
PIXABAY_ORIENTATION = os.getenv("PIXABAY_ORIENTATION", "horizontal")
PIXABAY_PER_PAGE = int(os.getenv("PIXABAY_PER_PAGE", "10"))
PIXABAY_MIN_SCORE = float(os.getenv("PIXABAY_MIN_SCORE", "0.78"))

OPENVERSE_ENABLED = _env_bool("OPENVERSE_ENABLED", True)
OPENVERSE_CLIENT_ID = os.getenv("OPENVERSE_CLIENT_ID")
OPENVERSE_CLIENT_SECRET = os.getenv("OPENVERSE_CLIENT_SECRET")
OPENVERSE_MIN_SCORE = float(os.getenv("OPENVERSE_MIN_SCORE", "0.70"))
OPENVERSE_PER_PAGE = int(os.getenv("OPENVERSE_PER_PAGE", "10"))

STOCK_IMAGE_TIMEOUT_SECONDS = int(os.getenv("STOCK_IMAGE_TIMEOUT_SECONDS", "10"))
STOCK_IMAGE_CACHE_HOURS = int(os.getenv("STOCK_IMAGE_CACHE_HOURS", "24"))
STOCK_IMAGE_REUSE_WINDOW_DAYS = int(os.getenv("STOCK_IMAGE_REUSE_WINDOW_DAYS", "20"))
STOCK_IMAGE_USAGE_PATH = os.getenv("STOCK_IMAGE_USAGE_PATH", "/app/cache/stock_image_usage.json")
STOCK_IMAGE_REUSE_CHECK_WP_HISTORY = _env_bool("STOCK_IMAGE_REUSE_CHECK_WP_HISTORY", True)

# Additive durable-state rollout controls. Apply maintenance/migrations Phase 2
# before enabling these in an environment with an existing database.
PROCESS_DURABLE_CLAIMS_ENABLED = _env_bool("PROCESS_DURABLE_CLAIMS_ENABLED", False)
PROCESS_CLAIM_TIMEOUT_MINUTES = int(os.getenv("PROCESS_CLAIM_TIMEOUT_MINUTES", "30"))
PUBLISH_DURABLE_STATE_ENABLED = _env_bool("PUBLISH_DURABLE_STATE_ENABLED", False)
PUBLISH_CLAIM_TIMEOUT_MINUTES = int(os.getenv("PUBLISH_CLAIM_TIMEOUT_MINUTES", "30"))

# Deterministic duplicate analysis is observational only. Apply the Phase 5
# manual migration before enabling its assessment reads and writes.
DUPLICATE_SHADOW_ENABLED = _env_bool("DUPLICATE_SHADOW_ENABLED", False)
DUPLICATE_LOOKBACK_HOURS = int(os.getenv("DUPLICATE_LOOKBACK_HOURS", "72"))
DUPLICATE_POLICY_VERSION = os.getenv("DUPLICATE_POLICY_VERSION", "v1").strip() or "v1"

# Phase 6A vector storage is a separate, optional MariaDB service. No pipeline
# path imports the vector store or opens this connection while disabled.
VECTOR_ENABLED = _env_bool("VECTOR_ENABLED", False)
VECTOR_DB_CONNECT_TIMEOUT_SECONDS = int(
    os.getenv("VECTOR_DB_CONNECT_TIMEOUT_SECONDS", "5")
)
VECTOR_DB_CONFIG = {
    "user": os.getenv("VECTOR_DB_USER"),
    "password": os.getenv("VECTOR_DB_PASSWORD"),
    "host": os.getenv("VECTOR_DB_HOST"),
    "port": int(os.getenv("VECTOR_DB_PORT", "3306")),
    "database": os.getenv("VECTOR_DB_NAME", "coincourier_vectors"),
    "connection_timeout": VECTOR_DB_CONNECT_TIMEOUT_SECONDS,
}

# WordPress REST API credentials
WP_API_URL = os.getenv("WP_API_URL")
WP_USERNAME = os.getenv("WP_USERNAME")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

# MySQL configuration for Flask API
DB_CONFIG = {
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'host': os.getenv('DB_HOST'),
    'port': int(os.getenv("DB_PORT", 3306)),
    'database': os.getenv('DB_NAME'),
    "ssl_disabled": False,
    "ssl_verify_cert": False
}

# MySQL configuration for WordPress DB
WP_DB_CONFIG = {
    'user': os.getenv('WP_DB_USER'),
    'password': os.getenv('WP_DB_PASSWORD'),
    'host': os.getenv('WP_DB_HOST'),
    'port': int(os.getenv("WP_DB_PORT", 3306)),
    'database': os.getenv('WP_DB_NAME'),
}
