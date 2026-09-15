"""Small, container-friendly logging setup."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})


load_dotenv()

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").strip().upper() or "INFO"
FILE_LOGGING_ENABLED = (
    (os.getenv("FILE_LOGGING_ENABLED") or "false").strip().lower() in TRUE_VALUES
)
_default_state_dir = (
    "/data"
    if (os.getenv("APP_ENV") or "development").strip().lower() == "production"
    else "/app/cache"
)
_state_dir = (os.getenv("WRITABLE_STATE_DIR") or _default_state_dir).strip()
FILE_LOG_PATH = os.getenv(
    "FILE_LOG_PATH",
    str(Path(_state_dir) / "logs" / "getnewsapi.log"),
)


def configure_logging(*, force: bool = False) -> None:
    """Configure stderr logging and optional explicitly enabled file logging."""

    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if FILE_LOGGING_ENABLED:
        path = Path(FILE_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        handlers=handlers,
        force=force,
    )
