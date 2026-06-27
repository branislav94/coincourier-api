"""
Configuration module.

Loads environment variables from a local .env file and exposes:
- API tokens/keys
- WordPress REST credentials
- Image generation settings
- MySQL connection dictionaries for the app DB and the WordPress DB

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

USE_API_IMAGES = int(os.getenv("USE_API_IMAGES", "1"))
IMAGE_MODEL   = os.getenv("IMAGE_MODEL", "gpt-image-1")
IMAGE_QUALITY = os.getenv("IMAGE_QUALITY", "high")
IMAGE_SIZE    = os.getenv("IMAGE_SIZE", "1024x1024") 
IMAGE_SOURCE_MODE = os.getenv("IMAGE_SOURCE_MODE", "openai").strip().lower()
OPENAI_IMAGE_FALLBACK = _env_bool("OPENAI_IMAGE_FALLBACK", True)

PEXELS_ENABLED = _env_bool("PEXELS_ENABLED", False)
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PEXELS_ORIENTATION = os.getenv("PEXELS_ORIENTATION", "landscape")
PEXELS_PER_PAGE = int(os.getenv("PEXELS_PER_PAGE", "10"))
PEXELS_MIN_SCORE = float(os.getenv("PEXELS_MIN_SCORE", "0.72"))

PIXABAY_ENABLED = _env_bool("PIXABAY_ENABLED", False)
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
PIXABAY_ORIENTATION = os.getenv("PIXABAY_ORIENTATION", "horizontal")
PIXABAY_PER_PAGE = int(os.getenv("PIXABAY_PER_PAGE", "10"))
PIXABAY_MIN_SCORE = float(os.getenv("PIXABAY_MIN_SCORE", "0.78"))

STOCK_IMAGE_TIMEOUT_SECONDS = int(os.getenv("STOCK_IMAGE_TIMEOUT_SECONDS", "10"))
STOCK_IMAGE_CACHE_HOURS = int(os.getenv("STOCK_IMAGE_CACHE_HOURS", "24"))


# OpenAI API Key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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
    'port': int(os.getenv("DB_PORT", 3306)),
    'database': os.getenv('WP_DB_NAME'),
}
