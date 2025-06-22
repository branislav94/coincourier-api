import requests
from db import get_db_connection
from config import CRYPTO_NEWS_TOKEN  # or define CRYPTO_NEWS_TOKEN here if you prefer

def fetch_general_news():
    """
    Fetch general crypto news (4 items) using the '/api/v1/category' endpoint.
    """
    url = 'https://cryptonews-api.com/api/v1/category'
    params = {
        'section': 'general',
        'items': 3,
        'page': 1,
        'token': CRYPTO_NEWS_TOKEN
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        data = response.json()
        return data.get('data', [])
    else:
        print(f'Error fetching general news: {response.status_code}')
        return []

def fetch_regulation_news():
    """
    Fetch regulation-related crypto news by adding text=regulation to the general endpoint.
    The API doesn't have a dedicated 'regulation' section, so we filter by text='regulation'.
    """
    url = 'https://cryptonews-api.com/api/v1/category'
    params = {
        'section': 'general',
        'text': 'regulation',
        'items': 3,
        'page': 1,
        'token': CRYPTO_NEWS_TOKEN
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        data = response.json()
        return data.get('data', [])
    else:
        print(f'Error fetching regulation news: {response.status_code}')
        return []

def fetch_bitcoin_news():
    """
    Fetch Bitcoin-specific news (4 items). Using 'tickers=BTC' returns articles mentioning BTC.
    """
    url = 'https://cryptonews-api.com/api/v1'
    params = {
        'tickers': 'BTC',   # or 'tickers-only': 'BTC' if you want ONLY BTC mentions
        'items': 3,
        'page': 1,
        'token': CRYPTO_NEWS_TOKEN
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        data = response.json()
        return data.get('data', [])
    else:
        print(f'Error fetching bitcoin news: {response.status_code}')
        return []

def record_exists(news_url, table):
    """
    Check if a record with the given news_url already exists in the specified table.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    query = f"SELECT COUNT(*) FROM {table} WHERE news_url = %s"
    cursor.execute(query, (news_url,))
    (count,) = cursor.fetchone()
    cursor.close()
    conn.close()
    return count > 0

def store_in_db(news_items):
    """
    Store raw news articles into the 'cryptonewsapi' table.
    Before inserting, check if the news_url already exists to avoid duplicates.
    The table has an auto-incrementing id as the primary key and a UNIQUE constraint on news_url.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    insert_sql = '''
        INSERT IGNORE INTO cryptonewsapi
        (news_url, title, full_text, publish_date, source_name, topics, sentiment, type, tickers, image_url, insertDate, processed)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0);
    '''
    
    from datetime import datetime
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for item in news_items:
        news_url = item.get('news_url')
        # Application-level duplicate check:
        if record_exists(news_url, "rich_crpytonews"):
            print("News already exists.")
            continue  # Skip insertion if the record already exists
        
        title = item.get('title')
        full_text = item.get('text', '')
        publish_date = item.get('date', None)
        source_name = item.get('source_name', '')
        topics = ', '.join(item.get('topics', []))
        sentiment = item.get('sentiment', None)
        news_type = item.get('type', '')
        tickers = ', '.join(item.get('tickers', []))
        image_url = item.get('image_url', '')
        
        cursor.execute(insert_sql, (news_url, title, full_text, publish_date, source_name, topics, sentiment, news_type, tickers, image_url, current_time))
        
    conn.commit()
    cursor.close()
    conn.close()

def fetch_all_news():
    """
    Fetch news from general, regulation, and bitcoin categories,
    then store them in the raw 'cryptonewsapi' table.
    """
    if backlog_exists():
        print("Backlog present ➜ skip external API fetch this cycle.")
        return
    general = fetch_general_news()
    regulation = fetch_regulation_news()
    bitcoin = fetch_bitcoin_news()
    
    all_news = general + regulation + bitcoin
    if all_news:
        store_in_db(all_news)
    else:
        print("No news fetched in this cycle.")

def backlog_exists() -> bool:
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT 1 FROM cryptonewsapi WHERE processed = 0 LIMIT 1")
    has = cur.fetchone() is not None
    cur.close(); conn.close()
    return has
