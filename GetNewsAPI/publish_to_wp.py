import base64
import mimetypes
import os
from datetime import datetime

import requests
import mysql.connector

from config import DB_CONFIG, WP_API_URL, WP_USERNAME, WP_APP_PASSWORD

session = requests.Session()
token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
session.headers.update({"Authorization": f"Basic {token}"})


def slugify(text: str) -> str:
    import re

    text = text.lower().strip()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


# ---------- DB helpers -------------------------------------------------------
def mark_news_as_published(news_url: str) -> None:
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE rich_crpytonews SET published = 1 WHERE news_url = %s",
            (news_url,),
        )
        conn.commit()


def fetch_unpublished(limit: int = 5) -> list[dict]:
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor(
        dictionary=True
    ) as cur:
        cur.execute(
            """
            SELECT * FROM rich_crpytonews
            WHERE published = 0
            ORDER BY insertDate DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


# ---------- WordPress helpers ------------------------------------------------
def ensure_category(name: str) -> int:
    """
    Return an existing WP category-ID for *name* or create it and return its id.
    """
    # First: try to fetch an existing term by slug
    slug = slugify(name)
    r = session.get(f"{WP_API_URL}/wp-json/wp/v2/categories", params={"slug": slug})
    r.raise_for_status()
    if r.json():
        return r.json()[0]["id"]

    # Otherwise create
    r = session.post(
        f"{WP_API_URL}/wp-json/wp/v2/categories",
        json={"name": name, "slug": slug},
    )
    r.raise_for_status()
    return r.json()["id"]


def upload_image(url: str) -> int | None:
    """
    Download the image from *url* and upload to WP Media library.
    Returns the attachment-ID or None on failure.
    """
    try:
        img_resp = session.get(url, timeout=10)
        img_resp.raise_for_status()
    except Exception as exc:
        print(f"⚠️  Could not download {url}: {exc}")
        return None

    filename = os.path.basename(url.split("?")[0])
    mime, _ = mimetypes.guess_type(filename)
    headers = {
        "Content-Disposition": f"attachment; filename={filename}",
        "Content-Type": mime or "application/octet-stream",
    }

    try:
        media_resp = session.post(
            f"{WP_API_URL}/wp-json/wp/v2/media",
            headers=headers,
            data=img_resp.content,
        )
        media_resp.raise_for_status()
        return media_resp.json()["id"]
    except Exception as exc:
        print(f"⚠️  Upload failed for {url}: {exc}")
        return None


# ---------- Main publisher ---------------------------------------------------
def publish_news_to_wp() -> None:
    news_items = fetch_unpublished()

    if not news_items:
        print("No new news items to publish.")
        return

    for item in news_items:
        # --------- image -----------------------------------------------------
        featured_id = None
        meta_data   = {}  
        if item.get("image_url"):
            featured_id = upload_image(item["image_url"])   # returns attachment-ID or None
            # keep the raw URL for reference
            meta_data["original_image_url"] = item["image_url"]

        # --------- category ---------------------------------------------------
        if item.get("tickers"):
            cat_name = item["tickers"]
        elif item.get("topics"):
            cat_name = item["topics"]
        else:
            cat_name = "General"

        category_id = ensure_category(cat_name)

        # --------- build post -------------------------------------------------
        post_data = {
            "title": item["title"],
            "content": item["full_text"],
            "status": "publish",
            "slug": slugify(item["title"]),
            "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "categories": [category_id],
             "meta":       meta_data   
        }
        if featured_id:
            post_data["featured_media"] = featured_id

        resp = session.post(f"{WP_API_URL}/wp-json/wp/v2/posts", json=post_data)
        if resp.status_code == 201:
            post_id = resp.json()["id"]
            print(f"✅ Published as WP post {post_id}")
            mark_news_as_published(item["news_url"])
        else:
            print("❌ Error publishing post:", resp.text)



if __name__ == '__main__':
    publish_news_to_wp()
