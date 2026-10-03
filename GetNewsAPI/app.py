from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import (
    API_DOCS_ENABLED,
    APP_ENV,
    DB_CONFIG,
    ENABLE_APSCHEDULER,
    PIPELINE_FRESH_START_AFTER_UTC_SQL,
    PUBLISH_API_TOKEN,
    READINESS_DB_TIMEOUT_SECONDS,
    VECTOR_DB_CONFIG,
    VECTOR_ENABLED,
)
from runtime.config_validation import format_config_issues, validate_runtime_config


logger = logging.getLogger(__name__)

INTERNAL_SERVER_ERROR_BODY = """<!doctype html>
<html lang=en>
<title>500 Internal Server Error</title>
<h1>Internal Server Error</h1>
<p>The server encountered an internal error and was unable to complete your request. Either the server is overloaded or there is an error in the application.</p>
"""

def get_db_connection():
    """Load the existing database entry point only when the route needs it."""
    from db import get_db_connection as connect

    return connect()


def publish_news_to_wp():
    """Load the existing publisher entry point only when the route needs it."""
    from publish_to_wp import publish_news_to_wp as publish

    return publish()


def get_readiness_db_connection():
    """Open a short-timeout application DB connection for a read-only probe."""
    import mysql.connector

    settings = dict(DB_CONFIG)
    settings["connection_timeout"] = READINESS_DB_TIMEOUT_SECONDS
    return mysql.connector.connect(**settings)


def get_vector_readiness_db_connection():
    """Open a short-timeout vector DB connection only when vector use is enabled."""
    import mysql.connector

    settings = dict(VECTOR_DB_CONFIG)
    settings["connection_timeout"] = READINESS_DB_TIMEOUT_SECONDS
    return mysql.connector.connect(**settings)


def start_scheduler():
    """Load and start the existing composite scheduler on lifespan entry."""
    from scheduler import start_scheduler as start

    return start()


def stop_scheduler() -> None:
    """Load and stop the existing composite scheduler on lifespan exit."""
    from scheduler import stop_scheduler as stop

    stop()


@asynccontextmanager
async def lifespan(application: FastAPI):
    scheduler_references: Any = None
    application.state.scheduler_references = None

    if APP_ENV == "production":
        issues = validate_runtime_config(profile="web")
        if issues:
            raise RuntimeError(
                "Invalid production configuration:\n" + format_config_issues(issues)
            )

    if PIPELINE_FRESH_START_AFTER_UTC_SQL:
        logger.info(
            "Pipeline fresh-start mode active after UTC %s",
            PIPELINE_FRESH_START_AFTER_UTC_SQL,
        )

    try:
        if ENABLE_APSCHEDULER:
            logger.info("APScheduler enabled")
            scheduler_references = start_scheduler()
            application.state.scheduler_references = scheduler_references
        else:
            logger.info("APScheduler disabled by ENABLE_APSCHEDULER=false")
        yield
    finally:
        try:
            if scheduler_references is not None:
                stop_scheduler()
        finally:
            application.state.scheduler_references = None


def unexpected_error(_request: Request, exc: Exception) -> HTMLResponse:
    logger.error(
        "Unhandled API request exception",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return HTMLResponse(content=INTERNAL_SERVER_ERROR_BODY, status_code=500)


def _require_publish_token(request: Request) -> None:
    if not PUBLISH_API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Publish API authentication is not configured.",
        )

    authorization = request.headers.get("Authorization", "")
    scheme, separator, candidate = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not candidate
        or not hmac.compare_digest(candidate, PUBLISH_API_TOKEN)
    ):
        raise HTTPException(
            status_code=401,
            detail="Valid Bearer authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def publish_news(request: Request) -> dict[str, str]:
    """Publish the latest prepared news items to WordPress."""
    _require_publish_token(request)
    publish_news_to_wp()
    return {"status": "success", "message": "News published to WordPress."}


def get_stored_news() -> list[dict[str, Any]]:
    """Return the seven most recent stored news rows."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM rich_crpytonews ORDER BY publish_date DESC LIMIT 7;")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def health() -> dict[str, str]:
    """Return process health without touching external systems."""
    return {"status": "ok", "service": "GetNewsAPI"}


def _probe_database(connect) -> None:
    connection = connect()
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


def ready() -> dict[str, Any] | JSONResponse:
    """Check core configuration and required databases without mutating them."""
    checks: dict[str, dict[str, Any]] = {}
    issues = validate_runtime_config(profile="web")
    if issues:
        checks["configuration"] = {
            "status": "failed",
            "errors": [issue.render() for issue in issues],
        }
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": checks},
        )

    checks["configuration"] = {"status": "ok"}
    try:
        _probe_database(get_readiness_db_connection)
        checks["application_database"] = {"status": "ok"}
    except Exception:
        checks["application_database"] = {"status": "unavailable"}
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": checks},
        )

    if VECTOR_ENABLED:
        try:
            _probe_database(get_vector_readiness_db_connection)
            checks["vector_database"] = {"status": "ok"}
        except Exception:
            checks["vector_database"] = {"status": "unavailable"}
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "checks": checks},
            )
    else:
        checks["vector_database"] = {"status": "skipped", "reason": "disabled"}

    return {"status": "ready", "checks": checks}


def create_app() -> FastAPI:
    docs_url = "/docs" if API_DOCS_ENABLED else None
    openapi_url = "/openapi.json" if API_DOCS_ENABLED else None
    application = FastAPI(
        title="GetNewsAPI",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url="/redoc" if API_DOCS_ENABLED else None,
        openapi_url=openapi_url,
    )
    application.add_exception_handler(Exception, unexpected_error)
    application.add_api_route(
        "/api/publish",
        publish_news,
        methods=["POST"],
        status_code=200,
    )
    application.add_api_route(
        "/api/news",
        get_stored_news,
        methods=["GET"],
        status_code=200,
    )
    application.add_api_route("/health", health, methods=["GET"], status_code=200)
    application.add_api_route(
        "/ready",
        ready,
        methods=["GET"],
        status_code=200,
        response_model=None,
    )
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from runtime.logging_config import configure_logging

    configure_logging(force=True)
    uvicorn.run(app, host="0.0.0.0", port=5000, workers=1, reload=False)
