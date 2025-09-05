# gpt_processor.py
import os, json, requests
from datetime import datetime
from db import get_db_connection
from config import OPENAI_API_KEY
import time, random
import requests
from typing import Any, Dict
import logging, pathlib
import re, hashlib
from html import escape
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(pathlib.Path(__file__).with_suffix('.log'),  # GetNewsAPI/gpt_processor.log
                       encoding="utf-8")
    ],
)

MAX_RETRIES   = 7
BASE_SLEEP    = 2 

TRANSITION_WORDS = {
    "however","therefore","moreover","furthermore","meanwhile","instead",
    "consequently","as a result","in addition","for example","for instance",
    "on the other hand","by contrast","thus","overall","finally","next",
    "then","first","second","third","additionally","notably","similarly",
    "likewise","nevertheless","nonetheless","in short","in summary","ultimately"
}

SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

# ---- static enum ------------------------------------------------------------
CATEGORIES = [
    "Bitcoin",        # 1
    "Ethereum",       # 2
    "Altcoins",       # 3 
    "Defi",           # 4
    "NFT",            # 5
    "Metaverse Gaming",# 6
    "Regulation",     # 7
    "Institutional",  # 8 banks, funds, ETFs
    "Markets",        # 9 price analysis, on-chain data
    "Security",       # 10 hacks, exploits, scams
    "Other"           # fallback
]

SEARCH_MODEL  = "gpt-4o-mini-search-preview-2025-03-11"
REWRITE_MODEL = "gpt-4.1"
USE_WEB_SEARCH = 1
# -----------------------------------------------------------------------------
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")   # <-- add this
GEMINI_SEARCH_MODEL = "models/gemini-2.5-flash"  # or gemini-1.5-pro-search
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"{GEMINI_SEARCH_MODEL}:generateContent?key={GOOGLE_API_KEY}"
)

import time, random
import requests
from typing import Any, Dict

MAX_RETRIES   = 7
BASE_SLEEP    = 2            # seconds   (be conservative)



def split_sentences(text_html: str) -> list[str]:
    # strip tags for readability stats
    text = re.sub(r"<[^>]+>", " ", text_html)
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]

def count_transition_sentences(sentences: list[str]) -> int:
    c = 0
    for s in sentences:
        low = s.lower()
        if any(t in low for t in TRANSITION_WORDS):
            c += 1
    return c

def first_paragraph(text_html: str) -> str:
    m = re.search(r"<p>(.*?)</p>", text_html, flags=re.I|re.S)
    return (m.group(1) if m else "").lower()

def h2_count(text_html: str) -> int:
    return len(re.findall(r"<h2>.*?</h2>", text_html, flags=re.I|re.S))

def word_count(text_html: str) -> int:
    txt = re.sub(r"<[^>]+>", " ", text_html)
    return len([w for w in re.findall(r"\b\w+\b", txt)])

def contains_external_link(text_html: str) -> bool:
    return bool(re.search(r'<a\s+[^>]*href="https?://', text_html, flags=re.I))

def contains_internal_link(text_html: str) -> bool:
    return bool(re.search(r'<a\s+[^>]*href="/[^"]*"', text_html, flags=re.I))

def keyphrase_count(text_html: str, keyphrase: str) -> int:
    if not keyphrase: return 0
    pat = re.escape(keyphrase.lower())
    body = re.sub(r"<[^>]+>", " ", text_html).lower()
    return len(re.findall(pat, body))



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

# before: GEMINI_URL = "https://…/v1beta/models/gemini-1.5-flash-latest:generateContent?key=…"

def call_gemini_search(prompt: str) -> str:
    """
    Ground the prompt with a live Google Search and return up to 3 bullet points.
    Uses Gemini's generateContent + google_search tool.
    """
    body = {
        "contents": [
            { "parts": [ { "text": prompt } ] }
        ],
        "tools": [
            { "google_search": {} }
        ]
        # you can optionally add:
        # ,"candidateCount": 1
        # ,"temperature": 0.1
    }

    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.post(GEMINI_URL, json=body, timeout=45)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if r.status_code in (429, 503):
            wait = BASE_SLEEP * attempt + random.random()
            print(f"⚠️  Gemini {r.status_code} – retrying in {wait:.1f}s")
            time.sleep(wait)
            continue
        # any other error
        print("❌ Gemini response:", r.text)
        r.raise_for_status()

    raise RuntimeError("Gemini search preview repeatedly failed")


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
    Build a richer English article and return strict JSON:
      title, full_text (HTML only), category, hashtags, sentiment,
      seo_focus, seo_slug, seo_meta, image_alt
    """
    enum_str = ", ".join(CATEGORIES)

    json_hint = (
        '{\n'
        '  "title": "Improved …",\n'
        '  "full_text": "<p>HTML only…</p>",\n'
        f'  "category": "<one to three of [{enum_str}]>",\n'
        '  "hashtags": "tag1, tag2, …",\n'
        '  "sentiment": 0.0,\n'
        '  "seo_focus": "bitcoin dominance",\n'
        '  "seo_slug":  "bitcoin-dominance-hits-4y-high",\n'
        '  "seo_meta":  "Concise 155-char description…",\n'
        '  "image_alt": "descriptive alt text…"\n'
        '}'
    )

    sys = (
        "You are a senior crypto journalist AND an SEO specialist.\n"
        "Rewrite the article as an engaging **news post** that strictly follows Yoast SEO best practices.\n"
        "\n"
        "FACT RULES (no hallucinations):\n"
        "• Use ONLY facts from ORIGINAL + EXTRA bullet points. If something is uncertain, hedge (e.g., “according to the source”).\n"
        "• Do NOT invent numbers, dates, quotes, or claims.\n"
        "\n"
        "FORMATTING (HTML ONLY – NO MARKDOWN, NO CODE FENCES):\n"
        "• Return 'full_text' as clean HTML only (<p>, <h2>, <strong>, <a>, <ul><li>). No <h1> inside the body.\n"
        "• Use <strong> for emphasis (not **).\n"
        "• All links must be <a href=\"…\">…</a>.\n"
        "\n"
        "YOAST SEO REQUIREMENTS:\n"
        "1) Length: 320–380 words.\n"
        "2) Focus keyphrase:\n"
        "   – Pick one and output as 'seo_focus'.\n"
        "   – Use it in the FIRST paragraph and ≥ 3 times overall (naturally distributed).\n"
        "3) SEO title (output as 'title'): ≤ 60 chars, compelling, includes keyphrase.\n"
        "4) Slug: 'seo_slug' in kebab-case, ≤ 60 chars, includes keyphrase, avoid stopwords if possible.\n"
        "5) Meta: 'seo_meta' 150–160 chars, includes keyphrase or close variant, reads like an ad.\n"
        "6) Image alt: 'image_alt' uses keyphrase or synonym; be descriptive.\n"
        "7) Internal link: include ONE natural internal link near the end to '/how-stablecoins-work/' (or a very close topic) with appropriate anchor text.\n"
        "8) Outbound link: include ONE credible external link (e.g., CoinMarketCap, Binance Academy, Wikipedia) with a relevant anchor.\n"
        "\n"
        "READABILITY TARGETS:\n"
        "• Subheadings: at least two <h2> subheads, well-distributed.\n"
        "• Sentences: ≥ 75% of sentences under 20 words.\n"
        "• Transition words in ≥ 30% of sentences (e.g., however, therefore, as a result, meanwhile, furthermore, in addition).\n"
        "• Active voice (keep passive voice minimal).\n"
        "• Short paragraphs; scannable structure; optional bullet list where it helps.\n"
        "\n"
        "SAFETY:\n"
        "• Do not include tracking parameters in links; keep anchors neutral.\n"
        "\n"
        "Return ONLY valid JSON matching:\n" + json_hint
    )

    chat = [
        {"role": "system", "content": sys},
        {
            "role": "user",
            "content": (
                f"EXTRA bullet-points (optional context):\n{extra_context}\n\n"
                f"ORIGINAL title: {article['title']}\n"
                f"ORIGINAL body:\n{article.get('text','')}"
            ),
        },
    ]

    raw_json = call_openai(REWRITE_MODEL, messages=chat)
    return json.loads(raw_json)

def validate_readability_and_seo(doc: dict) -> dict:
    """
    Check key Yoast-like constraints. Returns a dict with booleans and metrics.
    """
    text = doc.get("full_text", "")
    focus = (doc.get("seo_focus") or "").strip()
    sents = split_sentences(text)

    metrics = {
        "words": word_count(text),
        "h2s": h2_count(text),
        "sentences": len(sents),
        "over20": sum(1 for s in sents if len(s.split()) > 20),
        "transition_hits": count_transition_sentences(sents),
        "keyphrase_total": keyphrase_count(text, focus),
        "keyphrase_in_intro": (focus.lower() in first_paragraph(text)),
        "has_internal": contains_internal_link(text),
        "has_external": contains_external_link(text),
        "meta_len": len(doc.get("seo_meta","")),
        "title_len": len(doc.get("title","")),
    }

    pct_under20_ok = (metrics["sentences"] - metrics["over20"]) / max(1, metrics["sentences"])
    pct_transitions = metrics["transition_hits"] / max(1, metrics["sentences"])

    checks = {
        "len_ok": 320 <= metrics["words"] <= 380,
        "h2_ok": metrics["h2s"] >= 2,
        "sent_len_ok": pct_under20_ok >= 0.75,
        "transitions_ok": pct_transitions >= 0.30,
        "keyphrase_count_ok": metrics["keyphrase_total"] >= 3,
        "keyphrase_intro_ok": bool(metrics["keyphrase_in_intro"]),
        "internal_ok": metrics["has_internal"],
        "external_ok": metrics["has_external"],
        "meta_ok": 150 <= metrics["meta_len"] <= 160,
        "title_ok": metrics["title_len"] <= 60,
    }

    return {"metrics": metrics, "checks": checks}

def repair_if_needed(original_article: dict, extra_context: str, doc: dict) -> dict:
    status = validate_readability_and_seo(doc)
    if all(status["checks"].values()):
        return doc  # good

    # Ask the model to fix only what's missing, preserving facts.
    missing = [k for k,v in status["checks"].items() if not v]
    fix_instr = (
        "Fix ONLY the 'full_text', and if needed 'title', 'seo_meta', or 'seo_slug' so all failed checks pass.\n"
        f"Failed checks: {', '.join(missing)}.\n"
        "Preserve all facts; no new claims. Keep HTML-only body with <p>, <h2>, <strong>, <a>.\n"
        "Maintain the same 'seo_focus' value. Keep internal link to '/how-stablecoins-work/'.\n"
        "Return the FULL corrected JSON object."
    )

    chat = [
        {"role": "system", "content": "You repair JSON for SEO/readability; do not add new facts."},
        {"role": "user", "content": json.dumps(doc, ensure_ascii=False)},
        {"role": "user", "content": fix_instr},
    ]
    try:
        raw = call_openai(REWRITE_MODEL, messages=chat)
        fixed = json.loads(raw)
        return fixed
    except Exception:
        return doc  # fallback to original if repair fails


# ---------------- pipeline ---------------------------------------------------

def store_rich_news(record: dict, original: dict) -> None:


    """Insert/ignore enriched article into `rich_crpytonews` (now with SEO fields)."""

    from publish_to_wp import slugify        # reuse the helper

    record["seo_focus"] = record.get("seo_focus") or record["title"].lower()
    record["seo_slug"]  = record.get("seo_slug")  or slugify(record["title"])[:60]
    record["seo_meta"]  = record.get("seo_meta")  or record["title"][:155]

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
    """
    INSERT INTO rich_crpytonews
      (news_url, title, full_text, publish_date,
       source_name, category, hashtags, sentiment,
       tickers, image_url,
       seo_focus, seo_slug, seo_meta)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      seo_focus = VALUES(seo_focus),
      seo_slug  = VALUES(seo_slug),
      seo_meta  = VALUES(seo_meta)
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
            record["seo_focus"],
            record["seo_slug"],
            record["seo_meta"],
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
        draft = classify_and_rewrite(raw, extra)

        # one-shot repair if we miss key checks
        final_doc = repair_if_needed(raw, extra, draft)

        logging.info("SEO dump: %s", {k: final_doc[k] for k in ("seo_focus","seo_slug","seo_meta")})
        mark_processed(raw["news_url"])
        store_rich_news(final_doc, raw)
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
