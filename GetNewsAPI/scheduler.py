import logging
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from fetcher import fetch_all_news
from gpt_processor import process_news_with_gpt
from publish_to_wp import publish_news_to_wp  # Your function that calls WP REST API to insert posts

# Configure basic logging; you can adjust the level and format as needed.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def chained_job():
    logging.info("▶ starting chained job")
    logging.info("Starting fetch_all_news job.")
    try:
        fetch_all_news()
        logging.info("Finished fetching news successfully.")
    except Exception as e:
        logging.error("Error during fetch_all_news: %s", e)

    logging.info("Starting process_news_with_gpt job.")
    try:
        process_news_with_gpt()
        logging.info("Finished processing news with GPT successfully.")
    except Exception as e:
        logging.error("Error during process_news_with_gpt: %s", e)

    logging.info("Starting publish_news_to_wp job.")
    try:
        publish_news_to_wp()
        logging.info("Finished publishing news to WordPress successfully.")
    except Exception as e:
        logging.error("Error during publish_news_to_wp: %s", e)

def start_scheduler():
    scheduler = BackgroundScheduler()
    # Schedule the chained job to run every 12 hours, starting immediately.
    scheduler.add_job(
        chained_job,
        "interval",
        hours=1,
        next_run_time=datetime.now(),
        misfire_grace_time=3600,   # tolerate up to 1 h delay
        coalesce=True              # if multiple fires are overdue, run only once
    )    
    scheduler.start()
