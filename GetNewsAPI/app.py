import logging
from flask import Flask, jsonify
from db import get_db_connection
from scheduler import start_scheduler
from publish_to_wp import publish_news_to_wp
from config import ENABLE_APSCHEDULER, FLASK_DEBUG, PIPELINE_FRESH_START_AFTER_UTC_SQL


app = Flask(__name__)


@app.route('/api/publish', methods=['POST'])
def publish_news():
    """
    Publish the latest prepared news items to WordPress.

    Calls `publish_news_to_wp()` to push/publish news posts to the configured WordPress site.

    Args:
        None

    Returns:
        tuple[dict[str, str], int]:
            A JSON-style payload with:
                - status: 'success'
                - message: Human-readable confirmation
            And an HTTP status code (200).
    """
    publish_news_to_wp()
    return {"status": "success", "message": "News published to WordPress."}, 200

@app.route('/api/news', methods=['GET'])
def get_stored_news():
    """
    Return the most recent stored news rows from the database.

    Opens a DB connection via `get_db_connection()`, selects the latest 7 rows from
    `rich_crpytonews` ordered by `publish_date` descending, and returns them as a JSON response.

    Args:
        None

    Returns:
        flask.wrappers.Response:
            A Flask Response object produced by `jsonify(rows)` containing a JSON array of rows.
    """

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT * FROM rich_crpytonews ORDER BY publish_date DESC LIMIT 7;')
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(rows)
# Bane da ga duva
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    if PIPELINE_FRESH_START_AFTER_UTC_SQL:
        logging.info("Pipeline fresh-start mode active after UTC %s", PIPELINE_FRESH_START_AFTER_UTC_SQL)

    if ENABLE_APSCHEDULER:
        logging.info("APScheduler enabled")
        start_scheduler()
    else:
        logging.info("APScheduler disabled by ENABLE_APSCHEDULER=false")

    app.run(debug=FLASK_DEBUG, use_reloader=False, host="0.0.0.0", port=500)
