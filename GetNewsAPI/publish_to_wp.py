import base64
import mimetypes
import os
from datetime import datetime
import re
import requests
import mysql.connector
import io, tempfile
from PIL import Image as PILImage
from openai import OpenAI
import hashlib, random, time
from datetime import datetime, timedelta, timezone
from config import DB_CONFIG, WP_DB_CONFIG, WP_API_URL, WP_USERNAME, WP_APP_PASSWORD, USE_API_IMAGES

from config import DB_CONFIG, WP_DB_CONFIG, WP_API_URL, WP_USERNAME, WP_APP_PASSWORD

session = requests.Session()
token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
session.headers.update({
    "Authorization": f"Basic {token}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
})


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

POLICY_GUARD = (
    "no readable text, no logos/trademarks, no real people or public figures, "
    "no copyrighted characters"
)

# style families to avoid sameness (deterministic pick per post)
STYLE_FAMILIES = [
    "vibrant pixel art, crisp dithering, game-like UI accents",
    "flat vector illustration, bold gradients, editorial infographic vibe",
    "low-poly 3D render, soft global illumination, subtle depth-of-field",
    "clay render (plasticine look), studio softbox lighting",
    "neon cyberpunk glow, dark background, volumetric fog",
    "watercolor on textured paper, light ink outlines",
]

COMPOSITIONS = [
    "macro close-up subject filling frame",
    "wide establishing scene with layered depth",
    "top-down overhead diagrammatic view",
    "dramatic low-angle hero shot",
    "symmetrical center composition",
    "rule-of-thirds off-center layout",
]

CAMERA_MOVES = [
    "shallow depth-of-field",
    "tilt-shift miniature feel",
    "long-lens compression",
    "wide-angle perspective",
]

LIGHTING = [
    "soft studio lighting",
    "golden-hour rim light",
    "noir high-contrast lighting",
    "ambient skylight",
    "neon edge lights",
]

PALETTES_BULL = [
    "emerald green with charcoal accents",
    "teal and orange",
    "gold with midnight blue",
]
PALETTES_BEAR = [
    "crimson with slate gray",
    "infrared magenta and cyan",
    "amber with deep graphite",
]
PALETTES_NEUTRAL = [
    "pastel mint and lavender",
    "monochrome grayscale with a single accent color",
    "cool blues with soft neutrals",
]


MARKET_LINKS = {
    "Binance":        "https://www.binance.com",
    "OKX":            "https://www.okx.com",
    "Coinbase":       "https://www.coinbase.com",
    "Kraken":         "https://www.kraken.com",
    "Bitfinex":       "https://www.bitfinex.com",
    "KuCoin":         "https://www.kucoin.com",
    "Huobi":          "https://www.huobi.com",
    "Bitstamp":       "https://www.bitstamp.net",
    "Gemini":         "https://www.gemini.com",
    "Bybit":          "https://www.bybit.com",
    "Crypto.com":     "https://crypto.com/exchange",
    "Gate.io":        "https://www.gate.io",
}

API_BASE = WP_API_URL.rstrip("/")


IMG_MODEL   = os.getenv("IMAGE_MODEL", "gpt-image-1")
IMG_SIZE    = os.getenv("IMAGE_SIZE", "1024x1024")   # input square
IMG_QUALITY = os.getenv("IMAGE_QUALITY", "high")      # low | medium | high
IMAGE_SOURCE = "generate"





def slugify(text: str) -> str:
    import re

    text = text.lower().strip()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _stable_choice(key: str, options: list[str]) -> str:
    h = hashlib.md5(key.encode("utf-8")).digest()
    return options[h[0] % len(options)]


def _get_lock(conn, name: str, timeout: int = 1) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT GET_LOCK(%s,%s)", (name, timeout))
    got = (cur.fetchone() or (0,))[0] == 1
    cur.close()
    return got

def _release_lock(conn, name: str):
    cur = conn.cursor()
    cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
    _ = cur.fetchone()
    cur.close()

def _count_due_now() -> int:
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM rich_crpytonews r
            JOIN cryptonewsapi c ON c.news_url = r.news_url
            WHERE r.published = 0
              AND c.chosen_for_publish = 1
              AND c.scheduled_for IS NOT NULL
              AND c.scheduled_for <= UTC_TIMESTAMP()
        """)
        return int((cur.fetchone() or (0,))[0])
    
def _mood_from_title(title: str) -> str:
    t = title.lower()
    bull = any(w in t for w in ["surge", "rally", "jumps", "soars", "up", "gain", "bull"])
    bear = any(w in t for w in ["drops", "plunge", "falls", "down", "selloff", "bear"])
    if bull and not bear: return "bull"
    if bear and not bull: return "bear"
    return "neutral"

def _subject_from_tags(tags: set[str]) -> str:
    # several alternates per topic to reduce repetition
    if any(t in tags for t in ("bitcoin", "btc")):
        return _stable_choice("btc:"+",".join(sorted(tags)), [
            "physical gold coin with B-like glyph over candlestick dashboard",
            "blockchain city skyline forming a B-like silhouette",
            "mining rig silhouettes with glowing coin reflections",
        ])
    if any(t in tags for t in ("ethereum", "eth")):
        return _stable_choice("eth:"+",".join(sorted(tags)), [
            "faceted crystal token above connected network nodes",
            "abstract E-like shard hovering over smart contract code",
            "layered rollup highways converging into a crystal core",
        ])
    if "defi" in tags:
        return _stable_choice("defi", [
            "smart-contract scroll linking nodes in a decentralized web",
            "liquidity pools as glowing reservoirs connected by pipes",
            "yield farm fields with circuit-trace irrigation",
        ])
    if "nft" in tags:
        return _stable_choice("nft", [
            "digital gallery of framed pixel artworks and avatars",
            "collectible cards on a pedestal in a virtual room",
            "auction gavel over a holographic art grid",
        ])
    if any(t in tags for t in ("metaverse", "gaming", "metaverse gaming")):
        return _stable_choice("meta", [
            "VR headset over a grid city with floating tokens",
            "avatar portal doorway into voxel world",
            "HUD overlays in a virtual plaza with generic coins",
        ])
    if "regulation" in tags:
        return _stable_choice("reg", [
            "scales of justice beside blockchain nodes",
            "judge’s gavel and compliant checklist over ledger",
            "capitol building columns with chain of blocks",
        ])
    if any(t in tags for t in ("security", "hack", "hacks", "exploit", "scam")):
        return _stable_choice("sec", [
            "shield and padlock protecting a chain of blocks",
            "red-team versus blue-team on a network map",
            "firewall grid deflecting rogue packets",
        ])
    if any(t in tags for t in ("markets", "market", "price")):
        return _stable_choice("mkt", [
            "dashboard widgets with rising and falling candlesticks",
            "ticker board and orderbook panels in a trading desk",
            "macro arrows and heatmap tiles",
        ])
    if any(t in tags for t in ("institutional", "etf", "fund", "bank")):
        return _stable_choice("inst", [
            "bank-style columns with blockchain ledger and ticker",
            "ETF filing folder over market charts",
            "vault door with token stacks",
        ])
    if "altcoins" in tags:
        return _stable_choice("alts", [
            "many colorful generic tokens orbiting a hub",
            "token constellation connected by lines",
            "coin carousel with varied icons (generic)",
        ])
    # default
    return "stylized crypto scene with a generic coin and a blockchain network grid"

def build_image_prompt(title: str, hashtags: str) -> str:
    """
    More variety: style, composition, camera, lighting, palette vary per post (deterministically).
    Still avoids policy issues (no text/logos/real people).
    """
    # normalize tags from hashtags only (we avoid injecting brand names from titles)
    tags = {t.strip().lower() for t in re.split(r"[#,]", hashtags or "") if t.strip()}
    key = (title + "|" + ",".join(sorted(tags))).lower()

    style       = _stable_choice(key + ":style", STYLE_FAMILIES)
    composition = _stable_choice(key + ":comp", COMPOSITIONS)
    camera      = _stable_choice(key + ":cam", CAMERA_MOVES)
    lighting    = _stable_choice(key + ":light", LIGHTING)

    mood = _mood_from_title(title)
    if mood == "bull":
        palette = _stable_choice(key + ":pal", PALETTES_BULL)
    elif mood == "bear":
        palette = _stable_choice(key + ":pal", PALETTES_BEAR)
    else:
        palette = _stable_choice(key + ":pal", PALETTES_NEUTRAL)

    subject = _subject_from_tags(tags)

    # assemble — we hint at topic but forbid rendering text/logos/people
    return (
        f"{style}. {composition}; {camera}. {lighting}. "
        f"color palette: {palette}. "
        f"Subject: {subject}. "
        f"Keep it 16:9 friendly. {POLICY_GUARD}. "
        f"Theme inspired by the article’s topic; do not include any brand logos or text."
    )



def _heroize_to_1536x1024(raw_bytes: bytes) -> str | None:
    try:
        img = PILImage.open(io.BytesIO(raw_bytes)).convert("RGB")
        # upscale square → 1536x1536 then center-crop to 1536x1024
        img = img.resize((1536, 1536), PILImage.LANCZOS)
        top = (1536 - 1024) // 2
        img = img.crop((0, top, 1536, top + 1024))
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        img.save(tmp.name, format="JPEG", quality=90, optimize=True)
        return tmp.name
    except Exception as e:
        print(f"⚠️  heroize failed: {e}")
        return None


def build_edit_prompt(title: str, hashtags: str) -> str:
    # short + safe: restyle the *given* photo, no logos/people/text
    style = _stable_choice(title.lower() + ":style", STYLE_FAMILIES)
    return (
        f"Restyle this photograph into: {style}. "
        f"Abstract details so it’s not easily traceable to the source while preserving overall composition. "
        f"Remove any text/logos. {POLICY_GUARD}"
    )


def edit_image_from_url(url: str, title: str, hashtags: str) -> str | None:
    """
    Download a base photo, send to gpt-image-1 edits with a tiny prompt,
    return a local JPEG path (1536x1024) or None.
    """
    try:
        # 1) download base
        r = session.get(url, timeout=20)
        r.raise_for_status()
        base_bytes = r.content

        # 2) write to a temp file because the SDK likes file-like objects
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp_in:
            tmp_in.write(base_bytes)
            tmp_in.flush()
            tmp_in_path = tmp_in.name

        # 3) call edits
        prompt = build_edit_prompt(title, hashtags)
        with open(tmp_in_path, "rb") as f:
            resp = client.images.edit(
                model=os.getenv("IMAGE_MODEL", "gpt-image-1"),
                image=f,
                prompt=prompt,
                size=os.getenv("IMAGE_SIZE", "1024x1024"),
                quality=os.getenv("IMAGE_QUALITY", "high"),
                n=1,
            )


        # 4) decode image
        data0 = resp.data[0]
        if getattr(data0, "b64_json", None):
            raw = base64.b64decode(data0.b64_json)
        elif getattr(data0, "url", None):
            rr = session.get(data0.url, timeout=20); rr.raise_for_status()
            raw = rr.content
        else:
            print("⚠️  edits: empty response")
            return None

        # 5) resize+crop to WP hero (1536x1024) and save
        out = _heroize_to_1536x1024(raw)
        try:
            os.unlink(tmp_in_path)
        except OSError:
            pass
        return out
    except Exception as e:
        print(f"⚠️  edit_image_from_url failed: {e}")
        return None



# ---------- DB helpers -------------------------------------------------------
def mark_news_as_published(news_url: str) -> None:
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE rich_crpytonews SET published = 1 WHERE news_url = %s",
            (news_url,),
        )
        conn.commit()


def _today_bounds_utc():
    # Europe/Belgrade local midnight → UTC
    # If you already track LOCAL_TZ elsewhere, reuse that
    now = datetime.now(timezone.utc)
    # derive Belgrade midnight using fixed +02 offset for simplicity here
    # (use pytz/zoneinfo if DST accuracy is critical)
    today_local = (now + timedelta(hours=2)).date()
    start_local = datetime.combine(today_local, datetime.min.time()).replace(tzinfo=timezone.utc) - timedelta(hours=2)
    end_local   = start_local + timedelta(days=1)
    return start_local, end_local

def fetch_unpublished(limit: int = 10) -> list[dict]:
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            """
            SELECT r.*, c.scheduled_for, c.is_breaking
            FROM rich_crpytonews r
            JOIN cryptonewsapi c ON c.news_url = r.news_url
            WHERE r.published = 0
              AND c.chosen_for_publish = 1
              AND c.scheduled_for IS NOT NULL
              AND c.scheduled_for <= UTC_TIMESTAMP()
            ORDER BY c.scheduled_for ASC
            LIMIT %s
            """,
            (limit,),  # ← this comma was missing
        )
        rows = cur.fetchall()
        if rows:
            return rows

        # If you want *no* off-schedule posts, return [] here and delete the fallback block.
        cur.execute(
            """
            SELECT r.*
            FROM rich_crpytonews r
            LEFT JOIN cryptonewsapi c ON c.news_url = r.news_url
            WHERE r.published = 0
            ORDER BY COALESCE(c.is_breaking, 0) DESC,
                     COALESCE(c.final_importance, 0) DESC,
                     r.publish_date DESC
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

# ───────── WordPress DB helpers (new) ─────────────────────────────────────────
def get_wp_prefix(conn) -> str:
    """
    Detect the WP table-prefix once per run ( wp_, wp7_, etc. ).
    We look at any table that ends with 'options'.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name
        FROM   information_schema.tables
        WHERE  table_schema = DATABASE()
          AND  table_name LIKE '%options'
        LIMIT 1
    """)
    row = cur.fetchone()
    prefix = row[0].replace('options', '') if row else 'wp_'
    cur.close()
    return prefix


def upsert_postmeta(conn, prefix: str, post_id: int, key: str, value: str):
    """
    INSERT a post-meta row or UPDATE it if it already exists.
    """
    cur = conn.cursor()
    table = f"{prefix}postmeta"
    cur.execute(
        f"""INSERT INTO {table} (post_id, meta_key, meta_value)
            VALUES (%s,%s,%s)
            ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)
        """,
        (post_id, key, value)
    )
    conn.commit()
    cur.close()

def generate_image(title: str, hashtags: str) -> str | None:
    """
    GPT-Image-1: 1024x1024 (low) → upscale to 1536x1536 → center-crop to 1536x1024.
    Returns a local JPEG path or None.
    """
    prompt = build_image_prompt(title, hashtags)

    # 1) call OpenAI Images API
    try:
        resp = client.images.generate(
            model=IMG_MODEL,        # "gpt-image-1"
            prompt=prompt,
            size=IMG_SIZE,          # "1024x1024"
            quality=IMG_QUALITY,    # "low"
            n=1,
        )
    except Exception as e:
        print(f"⚠️  GPT-Image-1 request failed: {e}")
        return None

    # 2) extract bytes (b64 preferred; URL fallback)
    raw_bytes = None
    try:
        d = resp.data[0]
        if getattr(d, "b64_json", None):
            raw_bytes = base64.b64decode(d.b64_json)
        elif getattr(d, "url", None):
            r = session.get(d.url, timeout=20)
            r.raise_for_status()
            raw_bytes = r.content
        else:
            print("⚠️  GPT-Image-1 response had neither b64_json nor url.")
            return None
    except Exception as e:
        print(f"⚠️  GPT-Image-1 decode/download failed: {e}")
        return None

    # 3) post-process to 1536x1024 hero
    try:
        img = PILImage.open(io.BytesIO(raw_bytes)).convert("RGB")

        # upscale square to 1536x1536 (high-quality)
        target_w = target_h = 1536
        img = img.resize((target_w, target_h), PILImage.LANCZOS)

        # center-crop to 1536x1024 (landscape)
        crop_h = 1024
        top = (target_h - crop_h) // 2
        img = img.crop((0, top, target_w, top + crop_h))

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        img.save(tmp.name, format="JPEG", quality=90, optimize=True)
        return tmp.name
    except Exception as e:
        print(f"⚠️  GPT-Image-1 post-process failed: {e}")
        return None


RETRY_STATUS = {429, 500, 502, 503, 504}

def _post_with_retries(url: str, *, max_tries: int = 3, pause_s: int = 10, **kwargs) -> requests.Response:
    last = None
    for attempt in range(1, max_tries + 1):
        try:
            r = session.post(url, **kwargs)
            if r.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            if attempt < max_tries:
                print(f"⚠️  WP POST retry {attempt}/{max_tries} after {pause_s}s: {exc}")
                time.sleep(pause_s)
            else:
                print(f"❌  WP POST failed after {max_tries} tries: {exc}")
                raise


def upload_image(url: str | None, title: str, hashtags: str, *, force_generate: bool | None = None) -> int | None:
    if force_generate is None:
        force_generate = (USE_API_IMAGES == 0)

    content, filename = None, None

    # A) If we’re allowed to use the feed URL, try EDITS first
    if not force_generate and url:
        local = edit_image_from_url(url, title, hashtags)
        if local:
            with open(local, "rb") as fh:
                content = fh.read()
            filename = os.path.basename(local)
            try: os.unlink(local)
            except OSError: pass
            print("[IMG] Edited feed photo via gpt-image-1 (1024→1536x1024).")

    # B) If no content yet, fall back to GENERATION
    if content is None:
        local = generate_image(title, hashtags)  # your existing gpt-image-1 or DALL·E-2 path
        if local:
            with open(local, "rb") as fh:
                content = fh.read()
            filename = os.path.basename(local)
            try: os.unlink(local)
            except OSError: pass
            print("[IMG] Generated image (fallback).")
        else:
            print("⚠️  No image available — posting without featured image.")
            return None

    # C) Upload to WP (unchanged)
    mime, _ = mimetypes.guess_type(filename or "image.jpg")
    headers = {
        "Content-Disposition": f"attachment; filename={filename or 'image.jpg'}",
        "Content-Type": mime or "image/jpeg",
    }
    try:
        media_resp = _post_with_retries(f"{API_BASE}/wp-json/wp/v2/media", headers=headers, data=content)
        return media_resp.json()["id"]
    except Exception as exc:
        print(f"⚠️  Upload failed for {filename}: {exc}")
        return None




def set_media_alt(media_id: int, alt_text: str) -> None:
    try:
        session.post(
            f"{WP_API_URL}/wp-json/wp/v2/media/{media_id}",
            json={"alt_text": (alt_text or "")[:120]}
        ).raise_for_status()
    except Exception as exc:
        print(f"⚠️  Could not set alt text for media {media_id}: {exc}")

def ensure_term(name: str, taxonomy: str) -> int:
    # taxonomy: "categories" or "tags"
    slug = slugify(name)
    r = session.get(f"{API_BASE}/wp-json/wp/v2/{taxonomy}", params={"slug": slug})
    r.raise_for_status()
    if r.json():
        return r.json()[0]["id"]
    r = session.post(f"{API_BASE}/wp-json/wp/v2/{taxonomy}", json={"name": name, "slug": slug})
    r.raise_for_status()
    return r.json()["id"]


def link_markets(text: str) -> str:
    """
    Wrap every occurrence of a known exchange name in an <a> tag.
    Uses word-boundary regex, case-sensitive.
    """
    for name, url in MARKET_LINKS.items():
        # \b will match at word boundaries, except for names with punctuation (Crypto.com, OKX)
        pattern = r'(?<!["\'>])\b' + re.escape(name) + r'\b'
        replacement = f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
        text = re.sub(pattern, replacement, text)
    return text
# ---------- Main publisher ---------------------------------------------------
def publish_news_to_wp() -> None:
    # single-run guard (prevents overlapping scheduler/API runs)
    lock_conn = None
    try:
        lock_conn = mysql.connector.connect(**DB_CONFIG)
        if not _get_lock(lock_conn, "wp_publisher_lock", 1):
            print("[WP] Another publisher instance is running; skipping.")
            return

        due = _count_due_now()
        if due == 0:
            print("No new news items to publish.")
            return

        # cap per-run (env with sensible default)
        batch_cap = int(os.getenv("PUBLISH_BATCH_MAX", "10"))
        news_items = fetch_unpublished(limit=min(due, batch_cap))
        if not news_items:
            print("No new news items to publish.")
            return

        LOCAL_TZ = timezone(timedelta(hours=2))
        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

        for item in news_items:
            # ---- image ----
            featured_id = upload_image(
                item.get("image_url"),
                title=item["title"],
                hashtags=item.get("hashtags", ""),
            )
            meta_data = {}
            if featured_id:
                set_media_alt(featured_id, item.get("seo_focus") or item["title"])
            meta_data["_yoast_wpseo_focuskw"]  = item.get("seo_focus", "")
            meta_data["_yoast_wpseo_metadesc"] = item.get("seo_meta", "")

            # ---- categories ----
            cat_names = [c.strip() for c in item.get("category", "General").split(",") if c.strip()]
            category_ids = [ensure_category(name) for name in cat_names]

            # ---- content ----
            slug_source = item.get("seo_slug") or item["title"]
            with_links_content = link_markets(item["full_text"])
            category_url = f"/category/{slugify(cat_names[0])}/"
            with_links_content = with_links_content.replace(
                cat_names[0], f'<a href="{category_url}">{cat_names[0]}</a>', 1
            )

            # publish NOW (they’re due)
            wp_status = "publish"
            schedule_at_utc = now_utc

            local_when = schedule_at_utc.astimezone(LOCAL_TZ)
            print(f"Publishing: {item['title']!r} — UTC {schedule_at_utc:%Y-%m-%d %H:%M:%S} "
                  f"(Local {local_when:%Y-%m-%d %H:%M:%S})")

            post_data = {
                "title":      item["title"],
                "content":    with_links_content,
                "status":     wp_status,
                "slug":       slugify(slug_source)[:80],
                "date_gmt":   schedule_at_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                "categories": category_ids,
                # ✅ proper tags taxonomy
                "tags": [ensure_term(t.strip(), "tags") for t in item.get("hashtags", "").split(",") if t.strip()],
                "meta": meta_data,
            }
            if featured_id:
                post_data["featured_media"] = featured_id

            resp = session.post(f"{WP_API_URL}/wp-json/wp/v2/posts", json=post_data)
            if resp.status_code != 201:
                print("❌ Error publishing post:", resp.text)
                continue

            body = resp.json()
            post_id = body["id"]
            print(f"✅ Published WP post {post_id} at {schedule_at_utc:%Y-%m-%d %H:%M:%S}Z")

            # Yoast meta via DB
            with mysql.connector.connect(**WP_DB_CONFIG) as wp_conn:
                prefix = get_wp_prefix(wp_conn)
                if item.get("seo_focus"):
                    upsert_postmeta(wp_conn, prefix, post_id, '_yoast_wpseo_focuskw', item["seo_focus"])
                if item.get("seo_meta"):
                    upsert_postmeta(wp_conn, prefix, post_id, '_yoast_wpseo_metadesc', item["seo_meta"])

            mark_news_as_published(item["news_url"])

    finally:
        try:
            if lock_conn and getattr(lock_conn, "is_connected", lambda: False)():
                _release_lock(lock_conn, "wp_publisher_lock")
                lock_conn.close()
        except Exception:
            pass




if __name__ == '__main__':
    publish_news_to_wp()
