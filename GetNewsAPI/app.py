from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from config import ENABLE_APSCHEDULER, PIPELINE_FRESH_START_AFTER_UTC_SQL


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


app = FastAPI(title="GetNewsAPI", lifespan=lifespan)


@app.exception_handler(Exception)
def unexpected_error(_request: Request, exc: Exception) -> HTMLResponse:
    logger.error(
        "Unhandled API request exception",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return HTMLResponse(content=INTERNAL_SERVER_ERROR_BODY, status_code=500)


@app.post("/api/publish", status_code=200)
def publish_news() -> dict[str, str]:
    """Publish the latest prepared news items to WordPress."""
    publish_news_to_wp()
    return {"status": "success", "message": "News published to WordPress."}


@app.get("/api/news", status_code=200)
def get_stored_news() -> list[dict[str, Any]]:
    """Return the seven most recent stored news rows."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM rich_crpytonews ORDER BY publish_date DESC LIMIT 7;")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


@app.get("/health", status_code=200)
def health() -> dict[str, str]:
    """Return process health without touching external systems."""
    return {"status": "ok", "service": "GetNewsAPI"}


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=5000, workers=1, reload=False)
