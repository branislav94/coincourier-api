import base64
import logging
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
from typing import Any
from config import (
    DB_CONFIG,
    GROK_API_KEY,
    GROK_BASE_URL,
    GROK_IMAGE_ASPECT_RATIO,
    GROK_IMAGE_MODEL,
    GROK_IMAGE_RESOLUTION,
    IMAGE_FALLBACK_PROVIDER,
    IMAGE_SOURCE_PRIORITY,
    IMAGE_SOURCE_MODE,
    OPENAI_IMAGE_MODEL,
    OPENAI_IMAGE_FALLBACK,
    PIPELINE_FRESH_START_AFTER_UTC_SQL,
    PRIMARY_IMAGE_PROVIDER,
    STOCK_IMAGE_TIMEOUT_SECONDS,
    USE_API_IMAGES,
    USE_SOURCE_IMAGES,
    WP_API_URL,
    WP_APP_PASSWORD,
    WP_DB_CONFIG,
    WP_USERNAME,
)
from stock_images import StockImageCandidate, find_stock_image
from stock_images import record_stock_image_usage

session = requests.Session()
token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
session.headers.update({
    "Authorization": f"Basic {token}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
})

logger = logging.getLogger(__name__)


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


IMG_MODEL   = OPENAI_IMAGE_MODEL
IMG_SIZE    = os.getenv("IMAGE_SIZE", "1024x1024")   # input square
IMG_QUALITY = os.getenv("IMAGE_QUALITY", "high")      # low | medium | high
IMAGE_SOURCE = "generate"





def slugify(text: str) -> str:
    """
    Convert text into a lowercase, URL-safe slug.

    Replaces any run of non [a-z0-9] characters with a single '-' and trims
    leading/trailing dashes.

    Args:
        text: Input string.

    Returns:
        str:
            Slugified string suitable for URLs.
    """
    import re

    text = text.lower().strip()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _stable_choice(key: str, options: list[str]) -> str:
    """
    Pick a deterministic option from a list based on a stable hash of a key.

    Uses md5(key) and the first byte modulo len(options) to select an item.
    Intended to add variation per post while staying repeatable.

    Args:
        key: Stable selection key (e.g., title + tags).
        options: Non-empty list of candidate strings.

    Returns:
        str:
            Selected option.

    Raises:
        ZeroDivisionError:
            If options is empty.
    """
    h = hashlib.md5(key.encode("utf-8")).digest()
    return options[h[0] % len(options)]


def _get_lock(conn, name: str, timeout: int = 1) -> bool:
    """
    Acquire a MySQL named lock (GET_LOCK) to prevent overlapping runs.

    Args:
        conn: Open MySQL connection (mysql.connector connection).
        name: Lock name.
        timeout: Seconds to wait for the lock.

    Returns:
        bool:
            True if the lock was acquired, else False.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT GET_LOCK(%s,%s)", (name, timeout))
        got = (cur.fetchone() or (0,))[0] == 1
    finally:
        cur.close()
    return got

def _release_lock(conn, name: str) -> bool:
    """
    Release a MySQL named lock (RELEASE_LOCK).

    Args:
        conn: Open MySQL connection (mysql.connector connection).
        name: Lock name.

    Returns:
        bool:
            True if this connection released the lock, False otherwise.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
        released = (cur.fetchone() or (0,))[0]
        return released == 1
    finally:
        cur.close()


def _fresh_start_filter(alias: str = "c") -> tuple[str, tuple[str, ...]]:
    if not PIPELINE_FRESH_START_AFTER_UTC_SQL:
        return "", ()
    return f" AND {alias}.insertDate >= %s", (PIPELINE_FRESH_START_AFTER_UTC_SQL,)

def _count_due_now() -> int:
    """
    Count unpublished rich articles that are due to publish now (UTC).

    "Due" is defined as:
        - rich_crpytonews.published = 0
        - cryptonewsapi.chosen_for_publish = 1
        - cryptonewsapi.scheduled_for is not null
        - cryptonewsapi.scheduled_for <= UTC_TIMESTAMP()

    Args:
        None

    Returns:
        int:
            Number of due items.
    """
    fresh_start_clause, fresh_start_params = _fresh_start_filter("c")
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*)
            FROM rich_crpytonews r
            JOIN cryptonewsapi c ON c.news_url = r.news_url
            WHERE r.published = 0
              AND c.chosen_for_publish = 1
              AND c.scheduled_for IS NOT NULL
              AND c.scheduled_for <= UTC_TIMESTAMP()
              {fresh_start_clause}
        """, fresh_start_params)
        return int((cur.fetchone() or (0,))[0])
    
def _mood_from_title(title: str) -> str:
    """
    Classify title mood as bull/bear/neutral using simple keyword heuristics.

    Args:
        title: Article title.

    Returns:
        str:
            'bull', 'bear', or 'neutral'.
    """
    t = title.lower()
    bull = any(w in t for w in ["surge", "rally", "jumps", "soars", "up", "gain", "bull"])
    bear = any(w in t for w in ["drops", "plunge", "falls", "down", "selloff", "bear"])
    if bull and not bear: return "bull"
    if bear and not bull: return "bear"
    return "neutral"

def _subject_from_tags(tags: set[str]) -> str:
    """
    Choose a safe, generic image subject description based on normalized tags.

    Uses deterministic choices to reduce repetition while avoiding brand logos,
    readable text, real people, and copyrighted characters.

    Args:
        tags: Normalized lowercase tag set (e.g., {'bitcoin','markets'}).

    Returns:
        str:
            Subject description to be embedded in the image prompt.
    """
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
    Build a varied but policy-safe prompt for image generation.

    Variation is deterministic per post using stable choices across:
        - style family
        - composition
        - camera treatment
        - lighting
        - palette (bull/bear/neutral inferred from title)

    Safety constraints are embedded to avoid:
        - readable text
        - logos/trademarks
        - real people/public figures
        - copyrighted characters

    Args:
        title: Article title (used for mood + stable key; not for brand injection).
        hashtags: Comma-separated tags (may include '#', whitespace, etc.).

    Returns:
        str:
            A prompt string suitable for GPT-Image generation.
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
    """
    Convert image bytes into a WordPress hero JPEG (1536x1024), saved to a temp file.

    Flow:
        - Load bytes into PIL
        - Convert to RGB
        - Resize to 1536x1536
        - Center-crop to 1536x1024
        - Save as JPEG (quality=90) to a temp file

    Args:
        raw_bytes: Input image bytes.

    Returns:
        str | None:
            Path to the saved JPEG file, or None on failure.
    """
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


def _cover_bytes_to_1536x1024(
    raw_bytes: bytes,
    *,
    min_width: int = 640,
    min_height: int = 360,
) -> tuple[bytes, dict[str, Any]] | None:
    """
    Convert downloaded source/stock bytes into a 1536x1024 JPEG without distortion.
    """
    try:
        img = PILImage.open(io.BytesIO(raw_bytes)).convert("RGB")
        original_width, original_height = img.size
        if original_width < min_width or original_height < min_height:
            logger.info(
                "[IMG] rejected tiny image dims=%sx%s min=%sx%s",
                original_width,
                original_height,
                min_width,
                min_height,
            )
            return None

        ratio = original_width / max(original_height, 1)
        if ratio < 1.0:
            logger.info("[IMG] rejected portrait image dims=%sx%s", original_width, original_height)
            return None

        target_width, target_height = 1536, 1024
        scale = max(target_width / original_width, target_height / original_height)
        resized_width = max(target_width, int(round(original_width * scale)))
        resized_height = max(target_height, int(round(original_height * scale)))
        img = img.resize((resized_width, resized_height), PILImage.LANCZOS)

        left = (resized_width - target_width) // 2
        top = (resized_height - target_height) // 2
        img = img.crop((left, top, left + target_width, top + target_height))

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue(), {
            "width": original_width,
            "height": original_height,
            "output_width": target_width,
            "output_height": target_height,
        }
    except Exception:
        logger.exception("[IMG] could not convert downloaded image to hero JPEG")
        return None


def _download_external_hero_image(url: str, provider: str) -> tuple[bytes, dict[str, Any]] | None:
    """
    Download an external image and convert it to the WordPress hero format.
    """
    try:
        response = requests.get(
            url,
            timeout=STOCK_IMAGE_TIMEOUT_SECONDS,
            headers={"User-Agent": "CryptoCourierImageSelector/1.0"},
        )
        response.raise_for_status()
        converted = _cover_bytes_to_1536x1024(response.content)
        if not converted:
            logger.info("[IMG] %s image rejected after download url=%s", provider, url)
            return None
        content, dims = converted
        logger.info(
            "[IMG] %s image downloaded dims=%sx%s output=%sx%s",
            provider,
            dims.get("width"),
            dims.get("height"),
            dims.get("output_width"),
            dims.get("output_height"),
        )
        return content, dims
    except Exception:
        logger.exception("[IMG] %s image download failed url=%s", provider, url)
        return None


def build_edit_prompt(title: str, hashtags: str) -> str:
    """
    Build a short, policy-safe prompt for image editing/restyling.

    Intention: restyle a provided base photo into a chosen style family while:
        - removing text/logos
        - avoiding real people/public figures
        - making the result less traceable to the source image

    Args:
        title: Article title (used only for stable style choice).
        hashtags: Article hashtags (currently not strongly used; kept for extensibility).

    Returns:
        str:
            Editing prompt string.
    """
    # short + safe: restyle the *given* photo, no logos/people/text
    style = _stable_choice(title.lower() + ":style", STYLE_FAMILIES)
    return (
        f"Restyle this photograph into: {style}. "
        f"Abstract details so it’s not easily traceable to the source while preserving overall composition. "
        f"Remove any text/logos. {POLICY_GUARD}"
    )


def _source_images_enabled() -> bool:
    return bool(USE_SOURCE_IMAGES)


def _log_source_images_disabled() -> None:
    logger.info("[IMG] source images disabled by USE_SOURCE_IMAGES=false; skipping source image")


def edit_image_from_url(url: str, title: str, hashtags: str) -> str | None:
    """
    Download an image, restyle it via OpenAI image edits, and output a WP hero JPEG path.

    Flow:
        1) Download base image from url.
        2) Save to a temp file for the SDK.
        3) Call OpenAI images.edit with build_edit_prompt().
        4) Decode returned image (b64_json preferred, else URL download).
        5) Convert to 1536x1024 hero via _heroize_to_1536x1024().
        6) Clean up temp inputs.

    Args:
        url: Source image URL to download.
        title: Article title (prompt keying).
        hashtags: Hashtags for prompt context.

    Returns:
        str | None:
            Local JPEG path (1536x1024) on success, else None.

    Raises:
        requests.HTTPError:
            If the base image download fails with a non-2xx status and not caught upstream.
        Any exception may be caught internally and converted to None by this function.
    """

    if not _source_images_enabled():
        _log_source_images_disabled()
        return None

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
                model=IMG_MODEL,
                image=f,
                prompt=prompt,
                size=IMG_SIZE,
                quality=IMG_QUALITY,
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
    """
    Mark a rich article as published in `rich_crpytonews` by news_url.

    Args:
        news_url: Primary key linking rich_crpytonews to cryptonewsapi.

    Returns:
        None
    """
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE rich_crpytonews SET published = 1 WHERE news_url = %s",
            (news_url,),
        )
        conn.commit()


def _today_bounds_utc():
    """
    Compute today's bounds in UTC based on an assumed Europe/Belgrade local midnight.

    Note:
        This implementation uses a fixed +02:00 offset approximation.
        If DST correctness matters, replace with zoneinfo('Europe/Belgrade').

    Args:
        None

    Returns:
        tuple[datetime, datetime]:
            (start_utc, end_utc) for the local day converted to UTC.
    """
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
    """
    Fetch unpublished rich items that are due now.

    Primary query:
        - rich_crpytonews.published = 0
        - cryptonewsapi.chosen_for_publish = 1
        - cryptonewsapi.scheduled_for <= now (UTC)
        - ordered by scheduled_for ASC

    Args:
        limit: Max rows to return.

    Returns:
        list[dict]:
            List of rows (dicts) ready for publishing.
    """

    fresh_start_clause, fresh_start_params = _fresh_start_filter("c")
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            f"""
            SELECT r.*, c.scheduled_for, c.is_breaking
            FROM rich_crpytonews r
            JOIN cryptonewsapi c ON c.news_url = r.news_url
            WHERE r.published = 0
              AND c.chosen_for_publish = 1
              AND c.scheduled_for IS NOT NULL
              AND c.scheduled_for <= UTC_TIMESTAMP()
              {fresh_start_clause}
            ORDER BY c.scheduled_for ASC
            LIMIT %s
            """,
            (*fresh_start_params, limit),
        )
        rows = cur.fetchall()
        if rows:
            return rows

        return []



# ---------- WordPress helpers ------------------------------------------------
def ensure_category(name: str) -> int:
    """
    Ensure a WordPress category exists and return its term id.

    Behavior:
        - GET /wp/v2/categories?slug=<slugified(name)>
        - If found, returns the existing id.
        - Otherwise POST /wp/v2/categories to create and returns new id.

    Args:
        name: Category display name.

    Returns:
        int:
            WordPress category term id.

    Raises:
        requests.HTTPError:
            If the WordPress REST API returns an error.
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
    Detect the WordPress table prefix (wp_, wp7_, etc.) for the connected DB.

    Looks for any table in information_schema that ends with 'options' and derives
    the prefix by stripping 'options'. Falls back to 'wp_' if not found.

    Args:
        conn: Open MySQL connection to the WordPress database.

    Returns:
        str:
            Detected prefix string (including trailing underscore).
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
    Upsert a postmeta row into the WordPress database.

    Uses INSERT ... ON DUPLICATE KEY UPDATE to set meta_value for (post_id, meta_key).

    Args:
        conn: Open MySQL connection to the WordPress database.
        prefix: WordPress table prefix (e.g., 'wp_').
        post_id: WordPress post id.
        key: Meta key.
        value: Meta value to store.

    Returns:
        None
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
    Generate a new hero image using OpenAI Images and return a local JPEG path.

    Flow:
        - build_image_prompt(title, hashtags)
        - client.images.generate(IMG_MODEL, prompt, IMG_SIZE, IMG_QUALITY)
        - decode b64_json (preferred) or download from returned URL
        - resize to 1536x1536 then center-crop to 1536x1024
        - save to temp JPEG and return its path

    Args:
        title: Article title (prompt keying + mood inference).
        hashtags: Hashtags string (topic extraction for prompt).

    Returns:
        str | None:
            Local JPEG path (1536x1024) on success, else None.
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
            r = requests.get(
                d.url,
                timeout=20,
                headers={"User-Agent": "CryptoCourierImageSelector/1.0"},
            )
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


IMAGE_PROVIDERS = {"grok", "openai"}


def _safe_img_reason(exc: Exception) -> str:
    text = str(exc)
    for secret in (GROK_API_KEY, os.getenv("OPENAI_API_KEY")):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"xai-[A-Za-z0-9_-]+", "[redacted]", text)
    return f"{exc.__class__.__name__}: {' '.join(text.split())[:220]}"


def _normalize_image_provider(name: str | None, *, default: str = "openai") -> str:
    provider = (name or "").strip().lower()
    if provider not in IMAGE_PROVIDERS:
        logger.warning("[IMG] unknown image provider=%r; using provider=%s", provider, default)
        return default
    return provider


def _image_result_bytes(resp: Any, provider: str) -> bytes | None:
    try:
        data0 = resp.data[0]
        if getattr(data0, "b64_json", None):
            return base64.b64decode(data0.b64_json)
        if getattr(data0, "url", None):
            r = requests.get(
                data0.url,
                timeout=STOCK_IMAGE_TIMEOUT_SECONDS,
                headers={"User-Agent": "CryptoCourierImageSelector/1.0"},
            )
            r.raise_for_status()
            return r.content
    except Exception as exc:
        logger.warning("[IMG] provider=%s image decode/download failed reason=%s", provider, _safe_img_reason(exc))
        return None
    logger.warning("[IMG] provider=%s image response had no url or b64_json", provider)
    return None


def _grok_image_request(prompt: str):
    client_grok = OpenAI(
        api_key=GROK_API_KEY,
        base_url=GROK_BASE_URL,
        timeout=120,
    )
    extra_body = {}
    if GROK_IMAGE_ASPECT_RATIO:
        extra_body["aspect_ratio"] = GROK_IMAGE_ASPECT_RATIO
    if GROK_IMAGE_RESOLUTION:
        extra_body["resolution"] = GROK_IMAGE_RESOLUTION

    request_payload: dict[str, Any] = {
        "model": GROK_IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
    }
    if extra_body:
        request_payload["extra_body"] = extra_body

    try:
        return client_grok.images.generate(**request_payload)
    except TypeError as exc:
        if extra_body:
            logger.warning(
                "[IMG] provider=grok-image aspect/resolution not applied reason=%s",
                _safe_img_reason(exc),
            )
            request_payload.pop("extra_body", None)
            return client_grok.images.generate(**request_payload)
        raise
    except Exception as exc:
        if extra_body and any(key in str(exc).lower() for key in ("aspect_ratio", "resolution", "extra_body")):
            logger.warning(
                "[IMG] provider=grok-image aspect/resolution not applied reason=%s",
                _safe_img_reason(exc),
            )
            request_payload.pop("extra_body", None)
            return client_grok.images.generate(**request_payload)
        raise


def _grok_image_content(title: str, hashtags: str) -> tuple[bytes, str, dict[str, Any]] | None:
    if not GROK_API_KEY:
        logger.warning("[IMG] provider=grok-image unavailable reason=missing GROK_API_KEY")
        return None

    prompt = build_image_prompt(title, hashtags)
    try:
        resp = _grok_image_request(prompt)
    except Exception as exc:
        logger.warning("[IMG] provider=grok-image failed reason=%s", _safe_img_reason(exc))
        return None

    raw_bytes = _image_result_bytes(resp, "grok-image")
    if not raw_bytes:
        return None

    local = _heroize_to_1536x1024(raw_bytes)
    if not local:
        logger.warning("[IMG] provider=grok-image failed reason=post_process_failed")
        return None

    content, filename = _read_temp_image(local)
    logger.info(
        "[IMG] provider=grok-image selected model=%s fallback=False aspect_ratio=%s resolution=%s",
        GROK_IMAGE_MODEL,
        GROK_IMAGE_ASPECT_RATIO,
        GROK_IMAGE_RESOLUTION,
    )
    return content, filename, {
        "provider": "grok-image",
        "query": "",
        "score": None,
        "credit_text": "",
        "openai_fallback_used": False,
        "image_model": GROK_IMAGE_MODEL,
    }


def _openai_fallback_enabled() -> bool:
    return (
        OPENAI_IMAGE_FALLBACK
        and _normalize_image_provider(IMAGE_FALLBACK_PROVIDER, default="openai") == "openai"
    )


def _generated_image_content(
    provider: str,
    title: str,
    hashtags: str,
    *,
    fallback_used: bool,
) -> tuple[bytes, str, dict[str, Any]] | None:
    provider = _normalize_image_provider(provider, default="openai")
    if provider == "grok":
        selected = _grok_image_content(title, hashtags)
        if selected:
            content, filename, meta = selected
            meta["openai_fallback_used"] = False
            return content, filename, meta
        return None

    selected = _openai_image_content(
        None,
        title,
        hashtags,
        force_generate=True,
        fallback_used=fallback_used,
    )
    return selected


RETRY_STATUS = {429, 500, 502, 503, 504}

def _post_with_retries(url: str, *, max_tries: int = 3, pause_s: int = 10, **kwargs) -> requests.Response:
    """
    POST to WordPress with retries for transient HTTP statuses.

    Retries when status_code is in RETRY_STATUS: {429, 500, 502, 503, 504}.
    Sleeps pause_s between attempts.

    Args:
        url: Target URL to POST to.
        max_tries: Maximum attempts before failing.
        pause_s: Sleep between retries in seconds.
        **kwargs: Passed through to session.post().

    Returns:
        requests.Response:
            Successful response (2xx).

    Raises:
        requests.RequestException:
            If all retries fail or a non-retryable error occurs.
    """
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


def _legacy_upload_image(url: str | None, title: str, hashtags: str, *, force_generate: bool | None = None) -> int | None:
    """
    Produce and upload a featured image to WordPress, returning the media id.

    Selection:
        - If force_generate is None, defaults based on USE_API_IMAGES (env/config).
        - If using feed URL and allowed: try edit_image_from_url() first.
        - Otherwise fall back to generate_image().

    Upload:
        - Detect MIME type from filename
        - POST to /wp/v2/media with content bytes
        - Return created media id

    Args:
        url: Optional source image URL from the feed.
        title: Article title (for prompt context and deterministic styling).
        hashtags: Hashtags (for topic context).
        force_generate: If True, skip edits and always generate a new image.

    Returns:
        int | None:
            WordPress media id on success, else None.
    """
    if force_generate is None:
        force_generate = (USE_API_IMAGES == 0)

    content, filename = None, None
    source_edit_skipped = bool(not force_generate and url and not _source_images_enabled())

    # A) If we’re allowed to use the feed URL, try EDITS first
    if not force_generate and url and _source_images_enabled():
        local = edit_image_from_url(url, title, hashtags)
        if local:
            with open(local, "rb") as fh:
                content = fh.read()
            filename = os.path.basename(local)
            try: os.unlink(local)
            except OSError: pass
            print("[IMG] Edited feed photo via gpt-image-1 (1024→1536x1024).")

    if source_edit_skipped:
        _log_source_images_disabled()

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


def _read_temp_image(local_path: str) -> tuple[bytes, str]:
    with open(local_path, "rb") as fh:
        content = fh.read()
    filename = os.path.basename(local_path)
    try:
        os.unlink(local_path)
    except OSError:
        pass
    return content, filename


def _openai_image_content(
    url: str | None,
    title: str,
    hashtags: str,
    *,
    force_generate: bool,
    fallback_used: bool,
    allow_source_edit: bool = False,
) -> tuple[bytes, str, dict[str, Any]] | None:
    source_edit_allowed = allow_source_edit and _source_images_enabled()
    if source_edit_allowed and not force_generate and url:
        local = edit_image_from_url(url, title, hashtags)
        if local:
            content, filename = _read_temp_image(local)
            logger.info("[IMG] provider=openai selected fallback=%s source_edit=True", fallback_used)
            return content, filename, {
                "provider": "openai",
                "query": "",
                "score": None,
                "credit_text": "",
                "openai_fallback_used": fallback_used,
                "source_edit": True,
            }
    elif allow_source_edit and not force_generate and url:
        _log_source_images_disabled()

    local = generate_image(title, hashtags)
    if local:
        content, filename = _read_temp_image(local)
        logger.info("[IMG] provider=openai selected fallback=%s source_edit=False", fallback_used)
        return content, filename, {
            "provider": "openai",
            "query": "",
            "score": None,
            "credit_text": "",
            "openai_fallback_used": fallback_used,
            "source_edit": False,
        }
    return None


def _source_image_content(url: str | None) -> tuple[bytes, str, dict[str, Any]] | None:
    if not _source_images_enabled():
        _log_source_images_disabled()
        return None
    if not url:
        return None
    downloaded = _download_external_hero_image(url, "source")
    if not downloaded:
        logger.info("[IMG] source image rejected or unavailable")
        return None
    content, dims = downloaded
    logger.info(
        "[IMG] provider selected=source query=%r score=%.2f dims=%sx%s",
        "",
        1.0,
        dims.get("width"),
        dims.get("height"),
    )
    return content, "source-image.jpg", {
        "provider": "source",
        "query": "",
        "score": 1.0,
        "credit_text": "",
        "openai_fallback_used": False,
        "width": dims.get("width"),
        "height": dims.get("height"),
    }


def _maybe_source_image_content(url: str | None) -> tuple[bytes, str, dict[str, Any]] | None:
    if not _source_images_enabled():
        _log_source_images_disabled()
        return None
    return _source_image_content(url)


def _stock_image_content(article: dict[str, Any]) -> tuple[bytes, str, dict[str, Any]] | None:
    providers: tuple[str, ...] = ("pexels", "pixabay")
    candidate: StockImageCandidate | None = None

    while providers:
        candidate = find_stock_image(article, providers=providers)
        if not candidate:
            logger.info("[IMG] no stock image passed configured thresholds")
            return None

        downloaded = _download_external_hero_image(candidate.image_url, candidate.provider)
        if downloaded:
            break

        logger.info(
            "[IMG] accepted %s stock image could not be downloaded; trying next provider",
            candidate.provider,
        )
        current_index = providers.index(candidate.provider)
        providers = providers[current_index + 1 :]
    else:
        return None

    content, dims = downloaded
    logger.info(
        "[IMG] provider selected=%s query=%r score=%.2f credit=%r dims=%sx%s",
        candidate.provider,
        candidate.query,
        candidate.score,
        candidate.credit_name,
        dims.get("width"),
        dims.get("height"),
    )
    return content, f"{candidate.provider}-stock-image.jpg", {
        "provider": candidate.provider,
        "query": candidate.query,
        "score": candidate.score,
        "threshold": candidate.threshold,
        "credit_text": candidate.credit_text,
        "credit_name": candidate.credit_name,
        "credit_url": candidate.credit_url,
        "provider_asset_id": candidate.provider_asset_id,
        "asset_key": candidate.asset_key,
        "image_url_hash": hashlib.sha256(candidate.image_url.encode("utf-8")).hexdigest(),
        "openai_fallback_used": False,
        "width": dims.get("width"),
        "height": dims.get("height"),
    }


def upload_image(
    url: str | None,
    title: str,
    hashtags: str,
    *,
    article: dict[str, Any] | None = None,
    force_generate: bool | None = None,
) -> tuple[int | None, dict[str, Any]]:
    """
    Select, prepare, and upload a featured image to WordPress.

    Modes:
        openai: OpenAI image generation only.
        hybrid: priority-driven stock/source/generated selection with fallback.
        stock: Pexels/Pixabay with optional OpenAI fallback.
        source: source/feed image, with optional OpenAI fallback.
    """
    if force_generate is None:
        force_generate = (USE_API_IMAGES == 0)

    mode = (IMAGE_SOURCE_MODE or "hybrid").strip().lower()
    if mode not in {"openai", "hybrid", "stock", "source"}:
        logger.warning("[IMG] unknown IMAGE_SOURCE_MODE=%r; using hybrid", mode)
        mode = "hybrid"

    priority = (IMAGE_SOURCE_PRIORITY or "stock_first").strip().lower()
    if priority not in {"stock_first", "source_first", "openai_only", "grok_only"}:
        logger.warning("[IMG] unknown IMAGE_SOURCE_PRIORITY=%r; using stock_first", priority)
        priority = "stock_first"

    image_meta: dict[str, Any] = {
        "mode": mode,
        "priority": priority,
        "provider": None,
        "query": "",
        "score": None,
        "credit_text": "",
        "openai_fallback_used": False,
    }
    article_context = dict(article or {})
    article_context.setdefault("title", title)
    article_context.setdefault("hashtags", hashtags)

    primary_generated_provider = _normalize_image_provider(PRIMARY_IMAGE_PROVIDER, default="grok")
    fallback_generated_provider = _normalize_image_provider(IMAGE_FALLBACK_PROVIDER, default="openai")
    selected: tuple[bytes, str, dict[str, Any]] | None = None
    logger.info(
        "[IMG] mode=%s priority=%s use_source_images=%s primary=%s fallback=%s",
        mode,
        priority,
        _source_images_enabled(),
        primary_generated_provider,
        fallback_generated_provider,
    )

    if mode == "openai" or priority == "openai_only":
        selected = _generated_image_content("openai", title, hashtags, fallback_used=False)
    elif priority == "grok_only":
        selected = _generated_image_content("grok", title, hashtags, fallback_used=False)
        if not selected and _openai_fallback_enabled():
            logger.info("[IMG] provider=grok-image failed; trying provider=openai")
            selected = _generated_image_content("openai", title, hashtags, fallback_used=True)
    elif mode == "source":
        selected = _maybe_source_image_content(url)
        if not selected and _openai_fallback_enabled():
            logger.info("[IMG] provider=source failed; trying provider=openai")
            selected = _generated_image_content("openai", title, hashtags, fallback_used=True)
    elif mode == "stock":
        if priority == "source_first":
            selected = _maybe_source_image_content(url)
        if not selected:
            selected = _stock_image_content(article_context)
        if not selected and _openai_fallback_enabled():
            logger.info("[IMG] provider=stock failed; trying provider=openai")
            selected = _generated_image_content("openai", title, hashtags, fallback_used=True)
    else:
        if priority == "source_first":
            selected = _maybe_source_image_content(url)

        if not selected:
            selected = _stock_image_content(article_context)

        if not selected:
            logger.info(
                "[IMG] stock image unavailable; trying provider=%s",
                "grok-image" if primary_generated_provider == "grok" else primary_generated_provider,
            )
            logger.info(
                "[IMG] no fresh stock image passed thresholds; continuing to generated image provider=%s",
                "grok-image" if primary_generated_provider == "grok" else primary_generated_provider,
            )
            selected = _generated_image_content(
                primary_generated_provider,
                title,
                hashtags,
                fallback_used=False,
            )

        if not selected and USE_SOURCE_IMAGES and priority == "stock_first":
            selected = _maybe_source_image_content(url)

        if not selected and _openai_fallback_enabled() and primary_generated_provider != "openai":
            if primary_generated_provider == "grok":
                logger.info("[IMG] provider=grok-image failed; trying provider=openai")
            else:
                logger.info(
                    "[IMG] provider=%s failed; trying provider=openai",
                    primary_generated_provider,
                )
            selected = _generated_image_content("openai", title, hashtags, fallback_used=True)

        if not selected and not _openai_fallback_enabled():
            logger.info("[IMG] OpenAI image fallback disabled mode=%s priority=%s", mode, priority)

    if not selected:
        print("No image available - posting without featured image.")
        return None, image_meta

    content, filename, selected_meta = selected
    image_meta.update(selected_meta)

    mime, _ = mimetypes.guess_type(filename or "image.jpg")
    headers = {
        "Content-Disposition": f"attachment; filename={filename or 'image.jpg'}",
        "Content-Type": mime or "image/jpeg",
    }
    try:
        media_resp = _post_with_retries(f"{API_BASE}/wp-json/wp/v2/media", headers=headers, data=content)
        media_id = media_resp.json()["id"]
        logger.info(
            "[IMG] uploaded media_id=%s provider=%s mode=%s query=%r score=%s fallback=%s",
            media_id,
            image_meta.get("provider"),
            mode,
            image_meta.get("query"),
            image_meta.get("score"),
            image_meta.get("openai_fallback_used"),
        )
        return media_id, image_meta
    except Exception as exc:
        print(f"Upload failed for {filename}: {exc}")
        return None, image_meta


def set_media_details(
    media_id: int,
    alt_text: str,
    *,
    caption: str | None = None,
    description: str | None = None,
) -> None:
    payload: dict[str, str] = {"alt_text": (alt_text or "")[:120]}
    if caption:
        payload["caption"] = caption[:500]
    if description:
        payload["description"] = description[:1000]
    try:
        session.post(f"{WP_API_URL}/wp-json/wp/v2/media/{media_id}", json=payload).raise_for_status()
    except Exception as exc:
        print(f"Could not set media details for media {media_id}: {exc}")


def set_media_alt(media_id: int, alt_text: str) -> None:
    """
    Set the alt_text field for a WordPress media item via REST API.

    Args:
        media_id: WordPress media id.
        alt_text: Desired alt text (will be truncated to 120 chars).

    Returns:
        None
    """
    try:
        session.post(
            f"{WP_API_URL}/wp-json/wp/v2/media/{media_id}",
            json={"alt_text": (alt_text or "")[:120]}
        ).raise_for_status()
    except Exception as exc:
        print(f"⚠️  Could not set alt text for media {media_id}: {exc}")

def ensure_term(name: str, taxonomy: str) -> int:
    """
    Ensure a WordPress taxonomy term exists (category or tag) and return its id.

    Behavior:
        - GET /wp/v2/<taxonomy>?slug=<slug>
        - If found, returns id.
        - Otherwise POST to create and returns new id.

    Args:
        name: Term display name.
        taxonomy: 'categories' or 'tags'.

    Returns:
        int:
            Term id.

    Raises:
        requests.HTTPError:
            If the WordPress REST API returns an error.
    """
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
    Wrap occurrences of known exchange names with outbound <a> links.

    Uses a word-boundary regex replacement per exchange in MARKET_LINKS.
    Attempts to avoid replacing inside existing tags/attributes using a negative
    lookbehind for quote/angle-bracket patterns.

    Args:
        text: HTML string to modify.

    Returns:
        str:
            HTML with linked exchange names.
    """
    for name, url in MARKET_LINKS.items():
        # \b will match at word boundaries, except for names with punctuation (Crypto.com, OKX)
        pattern = r'(?<!["\'>])\b' + re.escape(name) + r'\b'
        replacement = f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
        text = re.sub(pattern, replacement, text)
    return text
# ---------- Main publisher ---------------------------------------------------
def publish_news_to_wp() -> None:
    """
    Publish due enriched news items to WordPress (featured image + Yoast SEO meta).

    Concurrency:
        - Uses MySQL named lock 'wp_publisher_lock' to prevent overlap.

    Flow:
        1) Acquire lock or exit.
        2) Count due items (_count_due_now); exit if none.
        3) Fetch due items (fetch_unpublished) capped by PUBLISH_BATCH_MAX.
        4) For each item:
            - Upload/edit/generate featured image (upload_image)
            - Set media alt text (set_media_alt)
            - Ensure categories exist (ensure_category)
            - Link exchange names in HTML (link_markets)
            - Optionally embed schema_jsonld as a <script type="application/ld+json"> block
            - POST to /wp/v2/posts with:
                title, content, status=publish, slug, date_gmt, categories, tags
            - On success: upsert Yoast SEO postmeta into WP DB (upsert_postmeta)
            - Mark item published in rich_crpytonews (mark_news_as_published)
        5) Release lock in finally.

    Args:
        None

    Returns:
        None
    """
    # single-run guard (prevents overlapping scheduler/API runs)
    lock_conn = None
    lock_acquired = False
    try:
        lock_conn = mysql.connector.connect(**DB_CONFIG)
        if not _get_lock(lock_conn, "wp_publisher_lock", 1):
            lock_holder = None
            try:
                cur_lock = lock_conn.cursor()
                cur_lock.execute("SELECT IS_USED_LOCK(%s)", ("wp_publisher_lock",))
                lock_holder = (cur_lock.fetchone() or (None,))[0]
                cur_lock.close()
            except Exception as exc:
                print(f"[WP] Could not inspect advisory lock 'wp_publisher_lock': {exc}")
            holder_suffix = f" (connection id={lock_holder})" if lock_holder else ""
            print(f"[WP] MySQL advisory lock 'wp_publisher_lock' is held by another DB session{holder_suffix}; skipping.")
            return
        lock_acquired = True
        print("[WP] Acquired MySQL advisory lock 'wp_publisher_lock'.")

        if PIPELINE_FRESH_START_AFTER_UTC_SQL:
            print(f"[WP] Pipeline fresh-start cutoff active: cryptonewsapi.insertDate >= {PIPELINE_FRESH_START_AFTER_UTC_SQL}")

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
            featured_id, image_meta = upload_image(
                item.get("image_url"),
                title=item["title"],
                hashtags=item.get("hashtags", ""),
                article=item,
            )
            if featured_id:
                credit_text = image_meta.get("credit_text") or ""
                set_media_details(
                    featured_id,
                    item.get("seo_focus") or item["title"],
                    caption=credit_text or None,
                    description=credit_text or None,
                )

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
            
            schema = item.get("schema_jsonld")
            if schema:
                with_links_content += f'\n<script type="application/ld+json">{schema}</script>\n'

            post_data = {
                "title":      item["title"],
                "content":    with_links_content,
                "status":     wp_status,
                "slug":       slugify(slug_source)[:80],
                "date_gmt":   schedule_at_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                "categories": category_ids,
                # ✅ proper tags taxonomy
                "tags": [ensure_term(t.strip(), "tags") for t in item.get("hashtags", "").split(",") if t.strip()],
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
            if featured_id:
                record_stock_image_usage(image_meta, post_id, item["title"])

            # Yoast SEO postmeta via DB. Do not rely on plugin REST meta exposure.
            with mysql.connector.connect(**WP_DB_CONFIG) as wp_conn:
                prefix = get_wp_prefix(wp_conn)
                upsert_postmeta(wp_conn, prefix, post_id, "_yoast_wpseo_focuskw", item.get("seo_focus", ""))
                upsert_postmeta(wp_conn, prefix, post_id, "_yoast_wpseo_metadesc", (item.get("seo_meta") or "")[:160])
                upsert_postmeta(wp_conn, prefix, post_id, "_yoast_wpseo_title", (item.get("title") or "")[:58])
                if item.get("seo_canonical"):
                    upsert_postmeta(wp_conn, prefix, post_id, "_yoast_wpseo_canonical", item["seo_canonical"])


            mark_news_as_published(item["news_url"])

    finally:
        try:
            if lock_acquired:
                if lock_conn and getattr(lock_conn, "is_connected", lambda: False)():
                    if _release_lock(lock_conn, "wp_publisher_lock"):
                        print("[WP] Released MySQL advisory lock 'wp_publisher_lock'.")
                    else:
                        print("[WP] RELEASE_LOCK('wp_publisher_lock') returned false; the lock may have already been released or lost with the DB connection.")
                else:
                    print("[WP] Could not release MySQL advisory lock 'wp_publisher_lock' because the owning DB connection is already closed.")
            if lock_conn and getattr(lock_conn, "is_connected", lambda: False)():
                lock_conn.close()
        except Exception as exc:
            print(f"[WP] Failed to release or close MySQL advisory lock connection for 'wp_publisher_lock': {exc}")




if __name__ == '__main__':
    publish_news_to_wp()
