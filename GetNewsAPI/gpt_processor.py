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

_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "CryptoCourier/1.0"
})


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
YOUTUBE_DOMAINS = ("youtube.com", "youtu.be")

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
REWRITE_MODEL = "gpt-5"
USE_WEB_SEARCH = 1


# Reasoning / style knobs for GPT-5 calls
REWRITE_TEMPERATURE = 1.0
REWRITE_REASONING   = {"effort": "high"}
REWRITE_VERBOSITY   = "low"   

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

def _count_due_within(minutes: int = 40) -> int:
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*)
        FROM cryptonewsapi
        WHERE processed = 0
          AND chosen_for_publish = 1
          AND scheduled_for IS NOT NULL
          AND scheduled_for <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE)
    """, (minutes,))
    n = int((cur.fetchone() or (0,))[0])
    cur.close(); conn.close()
    return n


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

def extract_source_urls(text_html: str) -> list[str]:
    """
    Return distinct http(s) URLs that appear in the ORIGINAL body.
    Excludes Wikipedia and tracking-polluted junk.
    """
    urls = re.findall(r'href="(https?://[^"]+)"', text_html, flags=re.I)
    # de-dup while preserving order
    seen, clean = set(), []
    for u in urls:
        low = u.lower()
        if "wikipedia.org" in low:
            continue
        if u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


def keyphrase_count(text_html: str, keyphrase: str) -> int:
    if not keyphrase: return 0
    pat = re.escape(keyphrase.lower())
    body = re.sub(r"<[^>]+>", " ", text_html).lower()
    return len(re.findall(pat, body))



def call_openai(
    model: str,
    *,
    timeout_read: int = 300,
    max_completion_tokens: int | None = 1200,
    **payload
) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "CryptoCourier/1.0",
    }

    payload["model"] = model
    payload["temperature"] = 1.0
    if model.startswith("gpt-5"):
        payload.setdefault("reasoning_effort", "high")
        payload.setdefault("verbosity", REWRITE_VERBOSITY)
    if max_completion_tokens is not None:
        payload["max_completion_tokens"] = max_completion_tokens

    backoff = BASE_SLEEP
    did_minimal_fallback = False  # <- ensure we only nudge once

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=(10, timeout_read))
        except (requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as e:
            if attempt == MAX_RETRIES: raise
            sleep = backoff + random.uniform(0, 1)
            logging.warning("OpenAI read timeout (%s). Retrying in %.1fs (attempt %d/%d)",
                            e.__class__.__name__, sleep, attempt, MAX_RETRIES)
            time.sleep(sleep); backoff *= 1.8
            continue
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES: raise
            sleep = backoff + random.uniform(0, 1)
            logging.warning("OpenAI request error %s. Retrying in %.1fs (attempt %d/%d)",
                            e.__class__.__name__, sleep, attempt, MAX_RETRIES)
            time.sleep(sleep); backoff *= 1.8
            continue

        if resp.status_code == 200:
            # ---- robust success handling -----------------------------------
            try:
                data = resp.json()
            except ValueError:
                logging.error("OpenAI: 200 but non-JSON body. Body (trimmed): %s", resp.text[:500])
                if attempt == MAX_RETRIES: raise
                sleep = backoff + random.uniform(0, 1)
                time.sleep(sleep); backoff *= 1.8
                continue

            try:
                choice = data["choices"][0]
                content = choice.get("message", {}).get("content", "")
                finish_reason = choice.get("finish_reason")
            except Exception:
                logging.error("OpenAI: malformed 200 response: %s", data)
                raise RuntimeError("OpenAI: missing content field")

            # If we hit output cap or got empty content, do a one-time fallback
            if (not content or not str(content).strip()) or (finish_reason == "length"):
                logging.error("OpenAI: empty/truncated content (finish_reason=%s). Usage: %s", 
                              finish_reason, data.get("usage"))
                if not did_minimal_fallback and attempt < MAX_RETRIES:
                    did_minimal_fallback = True
                    # make the model spend fewer reasoning tokens and allow more output
                    payload["reasoning_effort"] = "minimal"
                    payload["verbosity"] = "low"
                    # bump output budget (within your account’s per-call limits)
                    new_cap = max(int(payload.get("max_completion_tokens", 1200) * 1.6), 1800)
                    payload["max_completion_tokens"] = min(new_cap, 4096)  # cap it sensibly

                    # optional nudge so it goes straight to JSON
                    if isinstance(payload.get("messages"), list):
                        payload["messages"] = payload["messages"] + [
                            {"role": "system", "content": "Your last reply was truncated. "
                                                          "Return ONLY the final JSON object now—no commentary."}
                        ]

                    sleep = backoff + random.uniform(0, 1)
                    time.sleep(sleep); backoff *= 1.8
                    continue  # retry with leaner settings
                # already tried the fallback → hard fail so caller can handle
                raise RuntimeError("OpenAI: empty content after fallback")

            return content
            # ----------------------------------------------------------------

        if resp.status_code in (408, 429, 500, 502, 503, 504, 522, 524):
            retry_after = float(resp.headers.get("Retry-After", 0) or 0)
            sleep = max(backoff, retry_after) + random.uniform(0, 1)
            logging.warning("OpenAI HTTP %s. Retrying in %.1fs (attempt %d/%d)",
                            resp.status_code, sleep, attempt, MAX_RETRIES)
            if attempt == MAX_RETRIES: resp.raise_for_status()
            time.sleep(sleep); backoff *= 1.8
            continue

        logging.error("OpenAI error %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()

    raise RuntimeError("OpenAI API repeatedly failed")



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
        "Return max three concise bullet points with NEW facts, numbers or quotes "
        "that enrich the story. Prefer sources from the last 48 hours.\n\n"
        f"Title: {article['title']}\n\n"
        f"Body: {article.get('text','')[:1200]}"
    )
    return call_gemini_search(prompt).strip()




def classify_and_rewrite(article: dict, extra_context: str, video_url: str = "") -> dict:
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
        "ROLE: Produce a factual, engaging news post that strictly follows the OUTPUT CONTRACT.\n"
        "VERBOSITY: low. Keep prose lively but concise.\n"
        "\n"
        "FACT RULES (no hallucinations):\n"
        "• Use ONLY facts present in the ORIGINAL body and the EXTRA bullet points.\n"
        "• If something is uncertain, hedge (e.g., “according to the source”).\n"
        "\n"
        "FORMATTING (HTML ONLY – NO MARKDOWN, NO CODE FENCES):\n"
        "• Return 'full_text' as clean HTML only (<p>, <h2>, <strong>, <a>, <ul><li>). No <h1>.\n"
        "• Use <strong> for emphasis (not **). All links must be <a href=\"…\">…</a> without tracking params.\n"
        "\n"
        "VIDEO HANDLING:\n"
        "• If a YouTube VIDEO_URL is provided (non-empty), include exactly one <p>VIDEO_URL</p> at the most natural point.\n"
        "\n"
        "YOAST SEO REQUIREMENTS:\n"
        "1) Length: 320–380 words.\n"
        "2) Focus keyphrase:\n"
        "   – Pick one and output as 'seo_focus'.\n"
        "   – Use it in the FIRST paragraph and ≥ 3 times overall (naturally distributed).\n"
        "3) SEO title (output as 'title'): ≤ 60 chars; must include the keyphrase.\n"
        "4) Slug: 'seo_slug' in kebab-case, ≤ 60 chars, includes keyphrase.\n"
        "5) Meta: 'seo_meta' 150–160 chars, includes the keyphrase or close variant; read like a teaser ad.\n"
        "6) Image alt: 'image_alt' uses keyphrase or synonym; be descriptive.\n"
        "\n"
        "LINK POLICY (STRICT):\n"
        "• DO NOT add any internal links.\n"
        "• OUTBOUND link: include EXACTLY ONE only if it appears in ALLOWED_SOURCES below. "
        "Never invent or generalize links; no Wikipedia; no 'read our guide' placeholders. "
        "If ALLOWED_SOURCES is empty, include NO outbound link.\n"
        "• Do not add meta sentences like 'for background see …' unless you actually link to one of the allowed sources.\n"
        "\n"
        "READABILITY TARGETS:\n"
        "• Subheadings: at least two <h2> subheads, well-distributed.\n"
        "• Sentences: ≥ 75% under 20 words.\n"
        "• Transition words in ≥ 30% of sentences (however, therefore, as a result, meanwhile, etc.).\n"
        "• Active voice; short paragraphs; scannable structure; add a brief bullet list only if it adds clarity.\n"
        "\n"
        "SAFETY:\n"
        "• Neutral tone. No investment advice. No tracking parameters in links.\n"
        "\n"
        "OUTPUT CONTRACT:\n"
        f"{json_hint}\n"
        "Return ONLY valid JSON matching the contract keys. Do not include explanations."
    )


    orig_body = (article.get("text") or "")
    if len(orig_body) > 6000:
        orig_body = orig_body[:6000] + "…"

    allowed_sources = extract_source_urls(orig_body)
    allowed_block = "ALLOWED_SOURCES:\n" + ("\n".join(f"- {u}" for u in allowed_sources) if allowed_sources else "(none)")

    user_payload = (
        f"EXTRA bullet-points (optional context):\n{extra_context}\n\n"
        f"VIDEO_URL (empty if none): {video_url}\n\n"
        f"{allowed_block}\n\n"
        f"ORIGINAL title: {article['title']}\n"
        f"ORIGINAL body:\n{orig_body}"
    )

    chat = [
        {"role": "system", "content": sys},
        {"role": "user", "content": user_payload},
    ]

    raw_json = call_openai(
        REWRITE_MODEL,
        messages=chat,
        response_format={"type": "json_object"},
        timeout_read=300,
        max_completion_tokens=1800,           # give room for JSON
        reasoning_effort="minimal",           # <<< important
        verbosity="low"                       # helps keep output concise
    )

    return json.loads(raw_json)



def _normalize_youtube(url: str) -> str:
    # convert youtu.be/shorts to watch?v= for best oEmbed reliability
    try:
        u = url.strip()
        if "youtu.be/" in u:
            vid = u.split("youtu.be/")[1].split("?")[0].split("/")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        if "/shorts/" in u:
            vid = u.split("/shorts/")[1].split("?")[0].split("/")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        return u
    except Exception:
        return url

def inject_video_oembed(original: dict, html: str) -> str:
    """
    If it's a Video and a YouTube link, insert a plain URL on its own line after
    the first paragraph. WordPress will oEmbed it automatically.
    """
    try:
        if (original.get("type","").lower() == "video") and any(d in (original.get("news_url","").lower()) for d in YOUTUBE_DOMAINS):
            yurl = _normalize_youtube(original["news_url"])
            # insert right after the first closing </p>
            return re.sub(r"</p>", f"</p>\n<p>{yurl}</p>", html, count=1, flags=re.I)
    except Exception:
        pass
    return html

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
        # keep these as FYI (not enforced)
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
        # no link requirements
    }

    return {"metrics": metrics, "checks": checks}


def repair_if_needed(original_article: dict, extra_context: str, doc: dict) -> dict:
    status = validate_readability_and_seo(doc)
    if all(status["checks"].values()):
        return doc

    missing = [k for k,v in status["checks"].items() if not v]
    fix_instr = (
        "Fix ONLY the 'full_text', and if needed 'title', 'seo_meta', or 'seo_slug' so all failed checks pass.\n"
        f"Failed checks: {', '.join(missing)}.\n"
        "Preserve all facts; no new claims. Keep HTML-only body with <p>, <h2>, <strong>, <a>.\n"
        "Do NOT add any internal links. If adding an outbound link, it must be to a source URL that already exists in the ORIGINAL body; "
        "never add Wikipedia or generic placeholder links. If none exist, include no outbound link.\n"
        "Return the FULL corrected JSON object with the same keys as before."
    )

    chat = [
        {"role": "system", "content": "You repair JSON for SEO/readability; do not add new facts. VERBOSITY: medium."},
        {"role": "user", "content": json.dumps(doc, ensure_ascii=False)},
        {"role": "user", "content": fix_instr},
    ]
    try:
        raw = call_openai(
            REWRITE_MODEL,
            messages=chat,
            response_format={"type": "json_object"},
            timeout_read=100
        )
        fixed = json.loads(raw)
        return fixed
    except Exception as e:
        logging.warning("Repair failed, keeping original draft: %s", e)
        return doc


# ---------------- pipeline ---------------------------------------------------

def store_rich_news(record: dict, original: dict) -> None:
    """Insert/ignore enriched article into `rich_crpytonews` (now with SEO fields)."""
    from publish_to_wp import slugify  # reuse the helper

    record["seo_focus"] = record.get("seo_focus") or record["title"].lower()
    record["seo_slug"]  = (record.get("seo_slug")  or slugify(record["title"]))[:60]
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

def _maybe_video_url(original: dict) -> str:
    """
    Return a normalized YouTube URL only if the API item is a Video.
    Otherwise return '' so nothing is embedded.
    """
    try:
        if (original.get("type", "").lower() == "video"):
            u = (original.get("news_url") or "").strip()
            if any(d in u.lower() for d in YOUTUBE_DOMAINS):
                return _normalize_youtube(u)
    except Exception:
        pass
    return ""


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
        video_url = _maybe_video_url(raw)
        draft = classify_and_rewrite(raw, extra, video_url)
        final_doc = repair_if_needed(raw, extra, draft)

        if video_url and video_url not in final_doc.get("full_text", ""):
            final_doc["full_text"] = re.sub(r"</p>", f"</p>\n<p>{video_url}</p>", final_doc["full_text"], count=1, flags=re.I)

        logging.info("SEO dump: %s", {k: final_doc[k] for k in ("seo_focus","seo_slug","seo_meta")})
        store_rich_news(final_doc, raw)     # ✅ store first
        mark_processed(raw["news_url"])     # ✅ then mark processed
    except Exception as exc:
        print("⚠️ GPT pipeline failed:", exc)





def process_news_with_gpt(batch_size: int | None = None):
    if batch_size is None:
        # at least 3, at most 12 this run
        batch_size = max(3, min(12, _count_due_within(40)))

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT *
        FROM cryptonewsapi
        WHERE processed = 0
          AND chosen_for_publish = 1
        ORDER BY scheduled_for ASC, selected_at ASC
        LIMIT %s
    """, (batch_size,))
    articles = cur.fetchall()
    cur.close(); conn.close()

    for art in articles:
        process_one(art)



# ---- manual run -------------------------------------------------------------
if __name__ == "__main__":
    process_news_with_gpt()
