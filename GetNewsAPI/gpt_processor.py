# gpt_processor.py
import os, json, requests
from datetime import datetime
from db import get_db_connection
from config import OPENAI_API_KEY
import time, random
import requests
from typing import Any, Dict


MAX_RETRIES   = 7
BASE_SLEEP    = 2 

# ---- static enum ------------------------------------------------------------
CATEGORIES = [
    "bitcoin",        # 1
    "ethereum",       # 2
    "altcoins",       # 3 
    "defi",           # 4
    "nft",            # 5
    "metaverse_gaming",# 6
    "regulation",     # 7
    "institutional",  # 8 banks, funds, ETFs
    "markets",        # 9 price analysis, on-chain data
    "security",       # 10 hacks, exploits, scams
    "other"           # fallback
]

SEARCH_MODEL  = "gpt-4o-mini-search-preview-2025-03-11"
REWRITE_MODEL = "gpt-4.1-2025-04-14"
USE_WEB_SEARCH = 1
# -----------------------------------------------------------------------------
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")   # <-- add this
GEMINI_SEARCH_MODEL = "models/gemini-1.5-flash-latest"  # or gemini-1.5-pro-search
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"{GEMINI_SEARCH_MODEL}:generateContent?key={GOOGLE_API_KEY}"
)

import time, random
import requests
from typing import Any, Dict

MAX_RETRIES   = 7
BASE_SLEEP    = 2            # seconds   (be conservative)

def call_openai(model: str, **payload) -> str:
    url     = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload["model"] = model

    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.post(url, headers=headers, json=payload, timeout=40)

        # --- success --------------------------------------------------------
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]

        # --- rate-limit -----------------------------------------------------
        if resp.status_code == 429:
            # Respect any `Retry-After` header, else exponential back-off
            wait = float(resp.headers.get("Retry-After", BASE_SLEEP * attempt))
            wait += random.uniform(0, 1)          # jitter
            print(f"⚠️  429 – sleeping {wait:.1f}s (attempt {attempt})")
            time.sleep(wait)
            continue

        # --- other error ----------------------------------------------------
        resp.raise_for_status()   # will raise for 4xx/5xx other than 429

    # If we get here all retries failed
    raise RuntimeError("OpenAI API repeatedly returned 429")

def call_gemini_search(prompt: str) -> str:
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "tools": [
            { "webSearch": {} }
        ],
        "generationConfig": {"temperature": 0.1}
    }

    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.post(GEMINI_URL, json=body, timeout=45)

        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]

        if r.status_code in (429, 503):
            wait = BASE_SLEEP * attempt + random.uniform(0, 1)
            print(f"⚠️  Gemini {r.status_code} – sleep {wait:.1f}s")
            time.sleep(wait)
            continue

        print("❌ Gemini response:", r.text)
        r.raise_for_status()         # an        # any other error – crash

    raise RuntimeError("Gemini API repeatedly failed")

def enrich_with_search(article: dict) -> str:
    """Return ≤ 3 fresh bullet-points that complement the article."""
    if not USE_WEB_SEARCH:
        return ""

    prompt = (
        "You are a research assistant.\n"
        "Return **max three** bullet-points with NEW facts, numbers or quotes "
        "that enrich the story.\n\n"
        f"Title: {article['title']}\n\n"
        f"Body: {article.get('text','')[:1200]}"
    )
    return call_gemini_search(prompt).strip()




def classify_and_rewrite(article: dict, extra_context: str) -> dict:
    """
    Build a richer English article:

    • Weave `extra_context` (bullet-points) naturally into the body.
    • Return JSON with:
        - title
        - full_text
        - category  (1–3 comma-separated from CATEGORIES)
        - hashtags  (≤5, lower-case, no '#')
        - sentiment (-1‥1)
    """
    enum_str = ", ".join(CATEGORIES)

    json_hint = (
        '{\n'
        '  "title": "Improved …",\n'
        '  "full_text": "Merged & polished …",\n'
        f'  "category": "<one to three of [{enum_str}]>",\n'
        '  "hashtags": "tag1, tag2, …",\n'
        '  "sentiment": 0.0\n'
        '}'
    )

    chat = [
        {
            "role": "system",
            "content": (
                "You are a senior crypto journalist.\n\n"
                "Tasks:\n"
                "1. Read the ORIGINAL article and the EXTRA bullet-points.\n"
                "2. Rewrite/expand the article **in English** – keep it factual.\n"
                "3. Choose the BEST-FIT category (or up to three) from this list:\n"
                f"{enum_str}\n"
                "4. Generate up to five SEO-friendly hashtags (lower-case, no '#').\n"
                "5. Assess sentiment from -1 (very negative) to 1 (very positive).\n\n"
                "Return ONLY valid JSON matching this skeleton:\n"
                + json_hint
            ),
        },
        {
            "role": "user",
            "content": (
                f"EXTRA bullet-points:\n{extra_context}\n\n"
                f"ORIGINAL title: {article['title']}\n"
                f"ORIGINAL body:\n{article.get('text', '')}"
            ),
        },
    ]

    raw_json = call_openai(REWRITE_MODEL, messages=chat)
    return json.loads(raw_json)


# ---------------- pipeline ---------------------------------------------------

def store_rich_news(record: dict, original: dict):
    """Insert the enriched English article into `rich_crpytonews`."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        """
        INSERT IGNORE INTO rich_crpytonews
          (news_url, title, full_text, publish_date,
           source_name, category, hashtags, sentiment,
           tickers, image_url)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            original["news_url"],
            record["title"],
            record["full_text"],
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            original.get("source_name", ""),
            record["category"],
            record["hashtags"],
            record["sentiment"],
            ", ".join(original.get("tickers", [])),
            original.get("image_url", ""),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()

def mark_processed(news_url: str):
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE cryptonewsapi SET processed = 1 WHERE news_url = %s",
        (news_url,)
    )
    conn.commit()
    cur.close(); conn.close()

def process_one(raw):
    try:
        extra = enrich_with_search(raw)
        rewritten = classify_and_rewrite(raw, extra)
        mark_processed(raw["news_url"])
        store_rich_news(rewritten, raw)
    except Exception as exc:
        print("⚠️ GPT pipeline failed:", exc)


def process_news_with_gpt(batch_size: int = 40):
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM cryptonewsapi
        WHERE processed = 0
        ORDER BY publish_date DESC
        LIMIT %s
    """, (batch_size,))
    articles = cur.fetchall()
    cur.close(); conn.close()

    for art in articles:
        process_one(art)


# ---- manual run -------------------------------------------------------------
if __name__ == "__main__":
    process_news_with_gpt()
