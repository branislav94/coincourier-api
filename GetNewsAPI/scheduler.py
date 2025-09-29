# scheduler.py
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

from fetcher import start_scheduler as start_fetcher_scheduler
from gpt_processor import process_news_with_gpt
from publish_to_wp import publish_news_to_wp


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def chained_job():
    logging.info("▶ starting chained job")
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
    # start fetcher (runs immediately and then every 30 min)
    start_fetcher_scheduler()

    # run processor/publisher every 30 min, but start a little later than the fetcher
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        chained_job,
        "interval",
        minutes=30,
        next_run_time=datetime.now() + timedelta(minutes=3),  # ← offset so fetch finishes first
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
        jitter=10
    )
    scheduler.start()