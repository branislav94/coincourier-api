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
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

# Crypto News API token
CRYPTO_NEWS_TOKEN = os.getenv("CRYPTO_NEWS_TOKEN")

USE_API_IMAGES = int(os.getenv("USE_API_IMAGES", "1"))
IMAGE_MODEL   = os.getenv("IMAGE_MODEL", "gpt-image-1")
IMAGE_QUALITY = os.getenv("IMAGE_QUALITY", "high")
IMAGE_SIZE    = os.getenv("IMAGE_SIZE", "1024x1024") 


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
}

# MySQL configuration for WordPress DB
WP_DB_CONFIG = {
    'user': os.getenv('WP_DB_USER'),
    'password': os.getenv('WP_DB_PASSWORD'),
    'host': os.getenv('WP_DB_HOST'),
    'port': int(os.getenv("DB_PORT", 3306)),
    'database': os.getenv('WP_DB_NAME'),
}
