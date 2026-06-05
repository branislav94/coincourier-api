from flask import Flask, jsonify
from db import get_db_connection
from scheduler import start_scheduler
from publish_to_wp import publish_news_to_wp
from dotenv import load_dotenv


app = Flask(__name__)


@app.route('/api/publish', methods=['POST'])
def publish_news():
    publish_news_to_wp()
    return {"status": "success", "message": "News published to WordPress."}, 200

@app.route('/api/news', methods=['GET'])
def get_stored_news():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT * FROM rich_crpytonews ORDER BY publish_date DESC LIMIT 7;')
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(rows)
# Bane da ga duva
if __name__ == '__main__':
    start_scheduler()
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=500)
