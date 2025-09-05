import base64
import mimetypes
import os
from datetime import datetime
import re
import requests
import mysql.connector
import io, tempfile
from PIL import Image as PILImage
import openai
import hashlib, random, time

from config import DB_CONFIG, WP_DB_CONFIG, WP_API_URL, WP_USERNAME, WP_APP_PASSWORD

session = requests.Session()
token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
session.headers.update({
    "Authorization": f"Basic {token}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
})


openai.api_key = os.getenv("OPENAI_API_KEY")
IMG_MODEL = os.getenv("IMAGE_GEN_MODEL", "dall-e-3")

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


def slugify(text: str) -> str:
    import re

    text = text.lower().strip()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _stable_choice(key: str, options: list[str]) -> str:
    h = hashlib.md5(key.encode("utf-8")).digest()
    return options[h[0] % len(options)]

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

# ---------- DB helpers -------------------------------------------------------
def mark_news_as_published(news_url: str) -> None:
    with mysql.connector.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE rich_crpytonews SET published = 1 WHERE news_url = %s",
            (news_url,),
        )
        conn.commit()


def fetch_unpublished(limit: int = 10) -> list[dict]:
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
    Create an image with DALL·E / GPT Image.
    Returns a local image path or None on failure.
    """
    prompt = build_image_prompt(title, hashtags)

    # Prefer landscape for featured; fallback to square (cheaper). 
    # You can flip the order to put 1024x1024 first to save cost.
    sizes = ["1792x1024", "1024x1024"] if "dall-e-3" in (IMG_MODEL or "").lower() else ["1536x1024", "1024x1024"]

    last_exc = None
    for sz in sizes:
        try:
            resp = openai.images.generate(
                model=IMG_MODEL,
                prompt=prompt,
                n=1,
                size=sz,
                response_format="b64_json",
                # uncomment if you move to gpt-image-1:
                # quality="low",
                # background="auto",
            )
            b64 = resp.data[0].b64_json
            raw = base64.b64decode(b64)

            img = PILImage.open(io.BytesIO(raw)).convert("RGB")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            # JPEG is faster/lighter to upload; WP handles it fine.
            img.save(tmp.name, format="JPEG", quality=90, optimize=True)
            return tmp.name
        except Exception as exc:
            last_exc = exc
            print(f"⚠️  Image-gen failed at size {sz}: {exc}")

    print(f"⚠️  Image-gen failed (all sizes): {last_exc}")
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


def upload_image(url: str | None, title: str, hashtags: str) -> int | None:
    """
    Try original *url*; if it fails, generate an on-brand pixel-art image from title+hashtags.
    Upload to WP and return attachment ID (or None).
    """
    content: bytes | None = None
    filename: str | None = None

    # Try the source first
    if url:
        try:
            r = session.get(url, timeout=12)
            r.raise_for_status()
            content = r.content
            filename = os.path.basename(url.split("?")[0]) or "image.jpg"
        except Exception as exc:
            print(f"⚠️  Could not download {url}: {exc}")

    # Fallback: generate
    if content is None:
        local = generate_image(title, hashtags)
        if local:
            with open(local, "rb") as fh:
                content = fh.read()
            filename = os.path.basename(local)
            try:
                os.unlink(local)
            except OSError:
                pass


    if content is None:
        return None

    mime, _ = mimetypes.guess_type(filename or "image.png")
    headers = {
        "Content-Disposition": f"attachment; filename={filename}",
        "Content-Type": mime or "image/png",
    }

    try:
        media_resp = _post_with_retries(
            f"{API_BASE}/wp-json/wp/v2/media",
            headers=headers,
            data=content,
        )
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
    news_items = fetch_unpublished()

    if not news_items:
        print("No new news items to publish.")
        return

    for item in news_items:
        # --------- image -----------------------------------------------------
        featured_id = None
        meta_data   = {}  
        # always try — will generate if download fails
        featured_id = upload_image(
            item.get("image_url"),
            title=item["title"],
            hashtags=item.get("hashtags", "")
        )


        if featured_id:
            # feed Yoast’s "keyphrase in image alt" check
            set_media_alt(featured_id, item.get("seo_focus") or item["title"])

            meta_data["original_image_url"] = item["image_url"]

        meta_data["_yoast_wpseo_focuskw"]  = item.get("seo_focus", "")
        meta_data["_yoast_wpseo_metadesc"] = item.get("seo_meta", "")
        # --------- categories (one or many) ---------------------------------
        cat_names = [c.strip() for c in item.get("category", "General").split(",") if c.strip()]
        category_ids = [ensure_category(name) for name in cat_names]


        slug_source = item.get("seo_slug") or item["title"]     # ← new

        raw = item["full_text"]
        with_links_content = link_markets(raw)
        category_url = f"/category/{slugify(cat_names[0])}/"
        with_links_content = with_links_content.replace(
            cat_names[0],
            f'<a href="{category_url}">{cat_names[0]}</a>',
            1
        )
        # ---------- build post -------------------------------------------------
        post_data = {
            "title":   item["title"],
            "content": with_links_content,
            "status":  "publish",
            # <- use GPT-generated slug
            "slug": slugify(slug_source)[:80],                  # ← updated
            "date":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "categories": category_ids,
            "tags": [
                ensure_category(tag.strip())     # WP REST creates “post_tag” terms at same endpoint
                for tag in item.get("hashtags", "").split(",") if tag.strip()
            ],
            "meta": meta_data,
        }

        if featured_id:
            post_data["featured_media"] = featured_id

        resp = session.post(f"{WP_API_URL}/wp-json/wp/v2/posts", json=post_data)
        if resp.status_code == 201:
            post_id = resp.json()["id"]
            print(f"✅ Published as WP post {post_id}")

            # ── NEW: write Yoast meta directly into wp_postmeta ──────────────
            with mysql.connector.connect(**WP_DB_CONFIG) as wp_conn:
                prefix = get_wp_prefix(wp_conn)

                if item.get("seo_focus"):
                    upsert_postmeta(wp_conn, prefix, post_id,
                                    '_yoast_wpseo_focuskw', item["seo_focus"])

                if item.get("seo_meta"):
                    upsert_postmeta(wp_conn, prefix, post_id,
                                    '_yoast_wpseo_metadesc', item["seo_meta"])
            # -----------------------------------------------------------------

            mark_news_as_published(item["news_url"])
        else:
            print("❌ Error publishing post:", resp.text)



if __name__ == '__main__':
    publish_news_to_wp()
