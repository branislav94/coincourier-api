# gpt_processor.py
import os, json, requests
from datetime import datetime
from db import get_db_connection
from config import OPENAI_API_KEY, PIPELINE_FRESH_START_AFTER_UTC_SQL
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
REWRITE_TEMPERATURE = 1
REWRITE_REASONING   = {"effort": "high"}
REWRITE_VERBOSITY   = "low"   

PROCESS_LOOKAHEAD_MINUTES = int(os.getenv("PROCESS_LOOKAHEAD_MINUTES", "40"))
PROCESS_BATCH_MIN = int(os.getenv("PROCESS_BATCH_MIN", "3"))
PROCESS_BATCH_MAX = int(os.getenv("PROCESS_BATCH_MAX", "12"))

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
    """
    Split an HTML document into sentence-like chunks for readability metrics.

    Strips HTML tags, normalizes whitespace, then splits using SENTENCE_SPLIT_RE
    (punctuation boundary + whitespace).

    Args:
        text_html: HTML string to analyze.

    Returns:
        list[str]:
            Cleaned sentences (non-empty), trimmed of surrounding whitespace.
    """
    # strip tags for readability stats
    text = re.sub(r"<[^>]+>", " ", text_html)
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]

def count_transition_sentences(sentences: list[str]) -> int:
    """
    Count how many sentences contain at least one configured transition word/phrase.

    Transition words/phrases are matched as substring checks against lowercase sentences
    using TRANSITION_WORDS.

    Args:
        sentences: Sentence list to evaluate.

    Returns:
        int:
            Number of sentences that contain any transition marker.
    """
    c = 0
    for s in sentences:
        low = s.lower()
        if any(t in low for t in TRANSITION_WORDS):
            c += 1
    return c

def _count_due_within(minutes: int = 40) -> int:
    """
    Count queued items that are due to be processed within the next N minutes.

    Defines "due" as:
        processed = 0
        chosen_for_publish = 1
        scheduled_for is not null
        scheduled_for <= now + minutes (UTC)

    Args:
        minutes: Lookahead window in minutes.

    Returns:
        int:
            Count of due items.
    """
    fresh_start_clause = ""
    params: list[Any] = [minutes]
    if PIPELINE_FRESH_START_AFTER_UTC_SQL:
        fresh_start_clause = " AND insertDate >= %s"
        params.append(PIPELINE_FRESH_START_AFTER_UTC_SQL)

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute(f"""
        SELECT COUNT(*)
        FROM cryptonewsapi
        WHERE processed = 0
          AND chosen_for_publish = 1
          AND scheduled_for IS NOT NULL
          AND scheduled_for <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE)
          {fresh_start_clause}
    """, tuple(params))
    n = int((cur.fetchone() or (0,))[0])
    cur.close(); conn.close()
    return n


def first_paragraph(text_html: str) -> str:
    """
    Extract the first <p>...</p> inner text (lowercased) from an HTML string.

    Args:
        text_html: HTML content.

    Returns:
        str:
            Lowercased text inside the first paragraph, or '' if not found.
    """
    m = re.search(r"<p>(.*?)</p>", text_html, flags=re.I|re.S)
    return (m.group(1) if m else "").lower()

def h2_count(text_html: str) -> int:
    """
    Count <h2>...</h2> occurrences in an HTML string.

    Args:
        text_html: HTML content.

    Returns:
        int:
            Number of <h2> blocks found.
    """
    return len(re.findall(r"<h2>.*?</h2>", text_html, flags=re.I|re.S))

def word_count(text_html: str) -> int:
    """
    Estimate word count of an HTML string by stripping tags and counting word tokens.

    Args:
        text_html: HTML content.

    Returns:
        int:
            Number of word-like tokens.
    """
    txt = re.sub(r"<[^>]+>", " ", text_html)
    return len([w for w in re.findall(r"\b\w+\b", txt)])

def contains_external_link(text_html: str) -> bool:
    """
    Check whether the HTML contains at least one absolute http(s) link.

    Args:
        text_html: HTML content.

    Returns:
        bool:
            True if an <a href="http(s)://..."> link exists, else False.
    """
    return bool(re.search(r'<a\s+[^>]*href="https?://', text_html, flags=re.I))

def contains_internal_link(text_html: str) -> bool:
    """
    Check whether the HTML contains at least one root-relative internal link.

    Args:
        text_html: HTML content.

    Returns:
        bool:
            True if an <a href="/..."> link exists, else False.
    """
    return bool(re.search(r'<a\s+[^>]*href="/[^"]*"', text_html, flags=re.I))


def build_news_schema_jsonld(record: dict, original: dict) -> str:
    """
    Build a minimal NewsArticle JSON-LD string for WordPress/SEO plugins.

    Uses record fields (title, seo_meta) and original fields (news_url, image_url).
    Sets datePublished/dateModified to current UTC time.

    Args:
        record: Enriched article dict (expected keys: title, seo_meta, optionally image_url).
        original: Original API item dict (expected keys: news_url, image_url).

    Returns:
        str:
            JSON string for a NewsArticle schema, or '' on failure.
    """
    from html import unescape
    try:
        headline = (record.get("title") or "")[:110]
        desc     = (record.get("seo_meta") or "")[:160]
        url      = original.get("news_url") or ""
        img      = original.get("image_url") or ""
        pub_iso  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        data = {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": headline,
            "description": desc,
            "datePublished": pub_iso,
            "dateModified": pub_iso,
            "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        }
        if img:
            data["image"] = [img]
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return ""


def extract_source_urls(text_html: str) -> list[str]:
    """
    Extract distinct outbound http(s) URLs from HTML <a href="..."> attributes.

    Behavior:
        - Excludes wikipedia.org links.
        - Strips common tracking params (utm_*, fbclid, gclid, etc.).
        - Drops URL fragments.
        - Preserves insertion order of first occurrence.

    Args:
        text_html: HTML content to scan.

    Returns:
        list[str]:
            Cleaned, distinct URLs.
    """
    urls = re.findall(r'href="(https?://[^"]+)"', text_html, flags=re.I)
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    seen: set[str] = set()
    clean: list[str] = []

    for u in urls:
        try:
            low = u.lower()
            if "wikipedia.org" in low:
                continue

            sp = urlsplit(u)
            # drop utm_* and common tracking
            q = [
                (k, v)
                for (k, v) in parse_qsl(sp.query, keep_blank_values=True)
                if not (
                    k.lower().startswith("utm_")
                    or k.lower() in {"ref", "fbclid", "gclid", "mc_cid", "mc_eid"}
                )
            ]
            u_clean = urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), ""))  # drop fragment too

            if u_clean not in seen:
                seen.add(u_clean)
                clean.append(u_clean)

        except Exception:
            # Fallback: keep raw URL if parsing fails
            if u not in seen:
                seen.add(u)
                clean.append(u)

    return clean



def keyphrase_count(text_html: str, keyphrase: str) -> int:
    """
    Count occurrences of a keyphrase inside an HTML document (case-insensitive).

    Strips tags to text, lowercases, then counts regex matches of the escaped phrase.

    Args:
        text_html: HTML content.
        keyphrase: Target phrase to count.

    Returns:
        int:
            Number of matches found.
    """
    if not keyphrase: return 0
    pat = re.escape(keyphrase.lower())
    body = re.sub(r"<[^>]+>", " ", text_html).lower()
    return len(re.findall(pat, body))



def call_openai(
    model: str,
    *,
    timeout_read: int = 300,
    max_completion_tokens: int | None = 4096,
    phase: str = "openai",
    article_context: dict | None = None,
    **payload
) -> str:
    """
    Call OpenAI Chat Completions and return the assistant message content string.

    Reliability features:
        - Retries transient/network and 4xx/5xx retryable statuses with backoff + jitter.
        - On 200, validates JSON and extracts choices[0].message.content.
        - One-time fallback if content is empty or finish_reason == 'length':
            - reasoning_effort -> minimal
            - verbosity -> low
            - max_completion_tokens increased (capped)
            - adds a system nudge to return ONLY final JSON

    Args:
        model: Model name to use.
        timeout_read: Read timeout seconds for the request.
        max_completion_tokens: Max output tokens; None disables setting it.
        phase: Short processing phase label for diagnostics.
        article_context: Optional article metadata to include in failure logs.
        **payload: Additional chat completions payload fields (messages, response_format, etc.).

    Returns:
        str:
            choices[0].message.content.

    Raises:
        requests.HTTPError:
            For non-retryable HTTP errors or when retries are exhausted.
        RuntimeError:
            For malformed/empty responses after fallback handling.
        requests.exceptions.RequestException:
            When retries are exhausted for networking failures.
    """

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "CryptoCourier/1.0",
    }

    payload["model"] = model
    payload["temperature"] = payload.get("temperature", REWRITE_TEMPERATURE)
    if model.startswith("gpt-5"):
        payload.setdefault("reasoning_effort", "low")
        payload.setdefault("verbosity", REWRITE_VERBOSITY)
    if max_completion_tokens is not None:
        payload["max_completion_tokens"] = max_completion_tokens

    backoff = BASE_SLEEP
    did_minimal_fallback = False  # <- ensure we only nudge once
    log_context = {
        "phase": phase,
        "model": model,
        "article_id": (article_context or {}).get("id"),
        "title": ((article_context or {}).get("title") or "")[:160],
        "source": (article_context or {}).get("source_name"),
    }

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
                logging.error(
                    "OpenAI empty/truncated content. context=%s finish_reason=%s "
                    "max_completion_tokens=%s reasoning_effort=%s usage=%s",
                    log_context,
                    finish_reason,
                    payload.get("max_completion_tokens"),
                    payload.get("reasoning_effort"),
                    data.get("usage"),
                )
                if not did_minimal_fallback and attempt < MAX_RETRIES:
                    did_minimal_fallback = True
                    # make the model spend fewer reasoning tokens and allow more output
                    payload["reasoning_effort"] = "low"
                    payload["verbosity"] = "low"
                    # bump output budget (within your account’s per-call limits)
                    current_cap = int(payload.get("max_completion_tokens") or 0)
                    new_cap = max(current_cap * 2, 4096)
                    payload["max_completion_tokens"] = min(new_cap, 8192)

                    # optional nudge so it goes straight to JSON
                    if isinstance(payload.get("messages"), list):
                        payload["messages"] = payload["messages"] + [
                            {"role": "system", "content": "Your last reply was truncated. "
                                                          "Return ONLY the final JSON object now—no commentary."}
                        ]

                    logging.warning(
                        "Retrying OpenAI generation after truncation. context=%s "
                        "max_completion_tokens=%s reasoning_effort=%s",
                        log_context,
                        payload.get("max_completion_tokens"),
                        payload.get("reasoning_effort"),
                    )
                    sleep = backoff + random.uniform(0, 1)
                    time.sleep(sleep); backoff *= 1.8
                    continue  # retry with leaner settings
                # already tried the fallback → hard fail so caller can handle
                raise RuntimeError(f"OpenAI: empty/truncated content after fallback ({log_context})")

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
    Use Gemini generateContent with the google_search tool to ground the prompt.

    Behavior:
        - Sends the prompt as the only text part.
        - Enables google_search tool.
        - Retries 429/503 with incremental backoff.
        - Returns candidates[0].content.parts[0].text.

    Args:
        prompt: Research prompt to ground via search.

    Returns:
        str:
            Text response (intended to be up to ~3 bullet points).

    Raises:
        requests.HTTPError:
            If Gemini returns a non-retryable error.
        RuntimeError:
            If retries are exhausted.
        KeyError/IndexError:
            If the response structure is missing expected fields.
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
    """
    Fetch ≤3 fresh bullet points to complement the article using web search grounding.

    If USE_WEB_SEARCH is falsy, returns ''.

    Args:
        article: Original API item dict (expects keys: title, optionally text).

    Returns:
        str:
            Search-grounded bullet points text, or '' if disabled.
    """

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
    Rewrite an article into a richer English news post and return strict JSON fields.

    Output contract includes (at minimum):
        title, full_text (HTML), category, categories, hashtags, sentiment,
        seo_focus, seo_slug, seo_meta, image_alt
    Optional:
        schema_jsonld

    Constraints (enforced by prompt):
        - No hallucinations: use only ORIGINAL + EXTRA bullet points.
        - HTML-only body (no markdown), no <h1>.
        - Outbound links restricted to ALLOWED_SOURCES derived from ORIGINAL body (+ primary source).
        - If video_url provided, include exactly one <p>VIDEO_URL</p> at a natural point.

    Args:
        article: Original API item dict (expects title, text, news_url, etc.).
        extra_context: Search-grounded bullet points (may be empty).
        video_url: Normalized YouTube URL to embed ('' if none).

    Returns:
        dict:
            Parsed JSON object returned by the model.

    Raises:
        json.JSONDecodeError:
            If returned content is not valid JSON.
        requests.HTTPError / RuntimeError:
            From call_openai on repeated failures.
    """
    enum_str = ", ".join(CATEGORIES)

    json_hint = (
        '{\n'
        '  "title": "Improved …",\n'
        '  "full_text": "<p>HTML only…</p>",\n'
        f'  "category": "<one to three of [{enum_str}] (comma-separated, PRIMARY first)>",\n'
        '  "categories": ["Primary", "Secondary (optional)", "Tertiary (rare)"],\n'
        '  "hashtags": "tag1, tag2, …",\n'
        '  "sentiment": 0.0,\n'
        '  "seo_focus": "bitcoin dominance",\n'
        '  "seo_slug":  "bitcoin-dominance-hits-4y-high",\n'
        '  "seo_meta":  "Concise 145-160 char description…",\n'
        '  "image_alt": "descriptive alt text…"\n'
        '}'
    )

    sys = (
        "You are a senior crypto journalist AND an SEO specialist.\n"
        "ROLE: Produce a factual, engaging news post that strictly follows the OUTPUT CONTRACT.\n"
        "Tone: neutral, non-promotional. No investment advice or forward-looking claims.\n"
        "\n"
        "FACT RULES (no hallucinations):\n"
        "• Use ONLY facts present in the ORIGINAL body and the EXTRA bullet points.\n"
        "• If something is uncertain, hedge (e.g., “according to the source”).\n"
        "\n"
        "FORMATTING (HTML ONLY – NO MARKDOWN):\n"
        "• Return 'full_text' as clean HTML only (<p>, <h2>, <strong>, <a>, <ul><li>). No <h1>.\n"
        "• Use <strong> for emphasis. All links must be <a href=\"…\">…</a> with NO tracking parameters.\n"
        "\n"
        "VIDEO HANDLING:\n"
        "• If a YouTube VIDEO_URL is provided (non-empty), include exactly one <p>VIDEO_URL</p> at the most natural point.\n"
        "\n"
        "RANK MATH ON-PAGE TARGETS:\n"
        "• Focus keyword (your 'seo_focus') MUST appear in: SEO title, slug, meta description, first paragraph, at least one <h2>, and image alt.\n"
        "• Keep natural usage overall; aim ~0.8–1.5% density over the body (usually 5–10 mentions at 650–720 words).\n"
        "• Start the SEO title with the focus keyword; include a number and one soft power/positive/negative word when possible while staying factual.\n"
        "\n"
        "SEO LENGTHS:\n"
        "• Body length: 600–800 words (aim ~650–720).\n"
        "• SEO title: ≤ 58 characters (includes focus keyword).\n"
        "• Meta description: 145–160 characters (includes focus keyword or close variant; reads like a teaser).\n"
        "• Slug: ≤ 60 characters (kebab-case; includes focus keyword).\n"
        "\n"
        "LINK POLICY (STRICT):\n"
        "• Outbound: include at least one dofollow link, and only to a URL listed in ALLOWED_SOURCES (no tracking, no Wikipedia)."
        "• Internal links are optional here (the publisher may add one).\n"
        "• Never invent links; no Wikipedia; no generic 'read more'.\n"
        "\n"
        "READABILITY TARGETS:\n"
        "• Write 16–19 sentences; ≥ 75% under 20 words.\n"
        "• 30–40% of sentences should begin with transitions (however, therefore, meanwhile, as a result, in addition, by contrast, notably, etc.).\n"
        "• At least two <h2> subheads, well-distributed.\n"
        "• Use a short <ul><li> list only if it adds clarity.\n"
        "\n"
        "CATEGORY:\n"
        "• Choose 1–3 categories from: [Bitcoin, Ethereum, Altcoins, Defi, NFT, Metaverse Gaming, Regulation, Institutional, Markets, Security, Other].\n"
        "• STRONGLY prefer 1. Use 2 only if the story genuinely spans two pillars. Use 3 only in exceptional cross-domain cases.\n"
        "• Output:\n"
        "   – 'category' as a comma-separated string with PRIMARY first (e.g., \"Ethereum, Defi\").\n"
        "   – 'categories' as an array mirroring the same order.\n"
        "• If unsure between two pillars, pick the clearest single pillar rather than adding a second.\n"
        "\n"
        "SCHEMA (OPTIONAL):\n"
        "• Provide a valid JSON-LD string for NewsArticle (Rank Math auto-detects), using the given fields.\n"
        "\n"
        "SAFETY:\n"
        "• No advice, no price targets, no forward-looking statements.\n"
        "\n"
        "OUTPUT CONTRACT (JSON ONLY, EXACT KEYS; extra optional keys allowed):\n"
        "{\n"
        "  \"title\": \"Improved …\",                 // ≤58 chars; includes focus keyword\n"
        "  \"full_text\": \"<p>HTML only…</p>\",\n"
        "  \"category\": \"<1–3 from enum, comma-separated, PRIMARY first>\",\n"
        "  \"categories\": [\"Primary\", \"Secondary (optional)\", \"Tertiary (rare)\"],\n"
        "  \"hashtags\": \"tag1, tag2, …\",           // 3–6 tags, comma-separated (no #)\n"
        "  \"sentiment\": 0.0,                        // −1..1\n"
        "  \"seo_focus\": \"bitcoin dominance\",      // focus keyword\n"
        "  \"seo_slug\":  \"bitcoin-dominance-high\", // ≤60; includes focus keyword\n"
        "  \"seo_meta\":  \"Concise 145–160 char description…\", // includes focus keyword\n"
        "  \"image_alt\": \"descriptive alt text…\",  // includes keyword or synonym\n"
        "  \"schema_jsonld\": \"{...}\"               // OPTIONAL: valid NewsArticle JSON-LD string\n"
        "}\n"
        "Return ONLY valid JSON. No explanations."
        )



    orig_body = (article.get("text") or "")
    if len(orig_body) > 6000:
        orig_body = orig_body[:6000] + "…"

    allowed_sources = extract_source_urls(orig_body)

    # ✅ ensure at least the original source is allowed
    primary_src = (article.get("news_url") or "").strip()
    if primary_src:
        if primary_src not in allowed_sources:
            allowed_sources.insert(0, primary_src)

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
        max_completion_tokens=4096,           # give room for JSON plus GPT-5 reasoning
        phase="rewrite",
        article_context=article,
        reasoning_effort="low",           # <<< important
        verbosity="low"                       # helps keep output concise
    )

    return json.loads(raw_json)



def _normalize_youtube(url: str) -> str:
    """
    Normalize YouTube URLs to 'https://www.youtube.com/watch?v=...' form.

    Converts:
        - youtu.be/<id> -> watch?v=<id>
        - youtube.com/shorts/<id> -> watch?v=<id>

    Args:
        url: Input YouTube URL.

    Returns:
        str:
            Normalized URL, or original on parse failure.
    """
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
def _keyword_density(text_html: str, keyphrase: str) -> float:
    """
    Compute keyphrase density as a percentage of total word tokens.

    Args:
        text_html: HTML content.
        keyphrase: Focus keyword/phrase.

    Returns:
        float:
            Density percentage (0..100+).
    """
    if not keyphrase:
        return 0.0
    body = re.sub(r"<[^>]+>", " ", text_html)
    words = len(re.findall(r"\b\w+\b", body))
    hits  = keyphrase_count(text_html, keyphrase)
    return (hits / max(1, words)) * 100.0  # percent

def inject_video_oembed(original: dict, html: str) -> str:
    """
    Insert a YouTube URL as its own paragraph after the first paragraph for oEmbed.

    Only applies when:
        - original['type'] == 'video'
        - original['news_url'] is a YouTube domain

    Args:
        original: Original API item dict (expects type/news_url).
        html: Current HTML body.

    Returns:
        str:
            HTML with injected <p>youtube_url</p> after the first </p>, or unchanged.
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
    Evaluate Rank Math style SEO + readability checks and return metrics + boolean checks.

    Computes:
        - word count, sentence count, % sentences <= 20 words
        - transition opener ratio
        - h2 count
        - keyword placement in intro/title/slug/meta/h2/alt
        - keyword density range
        - title/meta length targets
        - outbound link presence

    Args:
        doc: Generated article JSON (expects full_text/title/seo_focus/seo_slug/seo_meta/image_alt).

    Returns:
        dict:
            {
              'metrics': {...computed numeric/context fields...},
              'checks': {...boolean pass/fail flags...}
            }
    """
    text = doc.get("full_text", "") or ""
    focus = (doc.get("seo_focus") or "").strip()
    title = (doc.get("title") or "")
    slug  = (doc.get("seo_slug") or "")
    meta  = (doc.get("seo_meta") or "")
    imgalt= (doc.get("image_alt") or "")

    sents = split_sentences(text)
    words = word_count(text)
    h2s   = h2_count(text)
    over20= sum(1 for s in sents if len(s.split()) > 20)
    pct_under20_ok = (len(sents) - over20) / max(1, len(sents))
    trans_hits = count_transition_sentences(sents)
    pct_transitions = trans_hits / max(1, len(sents))

    # links
    http_links = len(re.findall(r'<a\s+[^>]*href="https?://', text, flags=re.I))
    internal_links = len(re.findall(r'<a\s+[^>]*href="/[^"]*"', text, flags=re.I))

    # keyword placements
    intro_ok = (focus.lower() in first_paragraph(text))
    title_ok = (focus.lower() in title.lower())
    slug_ok  = (focus.lower() in slug.lower())
    meta_ok_kw = (focus.lower() in meta.lower())
    h2_has_kw = bool(re.search(rf"<h2>[^<]*{re.escape(focus)}[^<]*</h2>", text, flags=re.I))
    alt_ok = (focus.lower() in imgalt.lower())

    metrics = {
        "words": words,
        "h2s": h2s,
        "sentences": len(sents),
        "over20": over20,
        "transition_hits": trans_hits,
        "http_links": http_links,
        "internal_links": internal_links,
        "meta_len": len(meta),
        "title_len": len(title),
        "focus": focus
    }

    checks = {
        # lengths
        "len_ok": 600 <= words <= 800,
        "h2_ok": h2s >= 2,
        "sent_len_ok": pct_under20_ok >= 0.75,
        "transitions_ok": pct_transitions >= 0.30,

        # rank-math keyword placements
        "kw_in_intro": intro_ok,
        "kw_in_title": title_ok,
        "kw_in_slug": slug_ok,
        "kw_in_meta": meta_ok_kw,
        "kw_in_h2": h2_has_kw,
        "kw_in_alt": alt_ok,
        "kw_density_ok": 0.8 <= _keyword_density(text, focus) <= 1.5,

        # strict lengths for meta/title
        "meta_len_ok": 145 <= len(meta) <= 160,
        "title_len_ok": len(title) <= 58,

        # your link policy
        "at_least_one_external": http_links >= 1,
    }
    return {"metrics": metrics, "checks": checks}


def repair_if_needed(original_article: dict, extra_context: str, doc: dict) -> dict:
    """
    Attempt to repair a generated document that fails readability/SEO checks.

    Flow:
        - Runs validate_readability_and_seo(doc)
        - If all checks pass, returns doc unchanged
        - Otherwise asks the model to fix ONLY:
            full_text, and if needed title/seo_meta/seo_slug/image_alt
        - Returns fixed JSON on success; on failure raises so the article remains retryable.

    Args:
        original_article: Original API item dict (context only; not always used directly here).
        extra_context: Search-grounded bullet points used earlier (context only).
        doc: Current generated JSON to validate/repair.

    Returns:
        dict:
            Repaired doc if repair succeeds, else the original doc when no repair is needed.
    """

    status = validate_readability_and_seo(doc)
    if all(status["checks"].values()):
        return doc

    missing = [k for k,v in status["checks"].items() if not v]
    fix_instr = (
        "You are repairing JSON for Rank Math on-page SEO and readability.\n"
        "Fix ONLY: 'full_text' (structure, transitions, length) and, if needed, 'title', 'seo_meta', 'seo_slug', 'image_alt'.\n"
        f"Failed checks: {', '.join(missing)}.\n"
        "Rules:\n"
        "• Body 600–800 words; ≥75% of sentences under 20 words; aim 30–40% with transition openers.\n"
        "• Focus keyword ('seo_focus') must appear in: SEO title, slug, meta, first paragraph, ≥1 <h2>, and image alt; keep ~0.8–1.5% density.\n"
        "• Internal links: optional here (publisher may add one). Ensure AT LEAST ONE outbound link and only to ALLOWED_SOURCES.\n"
        "• Categories: choose 1–3 from the allowed enum; STRONGLY prefer 1; 2 only if genuinely cross-pillar; 3 only in exceptional cases. PRIMARY must be first.\n"
        "• Ensure 'categories' array and 'category' (comma-separated string) are consistent and deduplicated.\n"
        "• If under-length, ADD 1–3 short factual sentences derived ONLY from ORIGINAL and/or EXTRA; no new claims.\n"
        "• Keep HTML-only body (<p>, <h2>, <strong>, <a>, <ul><li>), no <h1>. Preserve existing facts.\n"
        "Return the FULL corrected JSON with the SAME keys (plus any existing optional keys like 'schema_jsonld')."
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
            timeout_read=100,
            max_completion_tokens=4096,
            phase="repair",
            article_context=original_article,
            reasoning_effort="low",
            verbosity="low"
        )
        fixed = json.loads(raw)
        return fixed
    except Exception:
        logging.exception(
            "Repair failed; article will remain retryable. article_id=%s title=%r source=%r",
            original_article.get("id"),
            original_article.get("title"),
            original_article.get("source_name"),
        )
        raise


def _fallback_meta(full_html: str, focus: str) -> str:
    """
    Create a fallback meta description snippet from the article body.

    Behavior:
        - Strips tags to text.
        - Takes an early snippet near 156 characters at a word boundary.
        - Ensures the focus keyword appears if provided (appends lightly).

    Args:
        full_html: HTML body.
        focus: Focus keyword/phrase.

    Returns:
        str:
            Meta description candidate (aiming for ~145–160 chars).
    """
    txt = re.sub(r"<[^>]+>", " ", full_html).strip()
    # start with the first ~156 chars at a word boundary
    snippet = (txt[:200] + "…") if len(txt) > 200 else txt
    snippet = snippet[:158].rsplit(" ", 1)[0]
    if focus and focus.lower() not in snippet.lower():
        # append a light-touch phrase including the focus keyword
        add = f" — {focus}" if len(snippet) <= 150 else f" {focus}"
        snippet = (snippet + add)[:160]
    return snippet

# ---------------- pipeline ---------------------------------------------------

def store_rich_news(record: dict, original: dict) -> None:
    """
    Upsert an enriched article into `rich_crpytonews` including SEO fields.

    Behavior:
        - Normalizes categories:
            * prefers record['categories'] if present, else parses record['category']
            * filters to known enum (CATEGORIES), de-dups, clamps to 1–3, defaults to 'Other'
            * sets both 'categories' (list) and 'category' (comma-separated string)
        - Fills missing seo_focus/seo_slug/seo_meta with safe defaults.
        - Inserts row keyed by news_url; on duplicate updates SEO fields.

    Args:
        record: Enriched JSON from the rewrite step (expects title/full_text/category/hashtags/sentiment/seo_*).
        original: Original API item dict (expects news_url/source_name/tickers/image_url).

    Returns:
        None
    """
    from publish_to_wp import slugify  # reuse the helper


    # Normalize categories: primary first, comma-space separated, 1–3 max, valid enum
    enum = set([c.strip() for c in CATEGORIES])
    cat_str = (record.get("category") or "").strip()
    cat_list = [c.strip() for c in cat_str.split(",") if c.strip()]
    # If an array is present, prefer it
    if isinstance(record.get("categories"), list) and record["categories"]:
       cat_list = [str(c).strip() for c in record["categories"] if str(c).strip()]
   # De-dup and filter to enum
    seen=set(); cat_list=[c for c in cat_list if (c in enum) and not (c in seen or seen.add(c))]
    if not cat_list: cat_list = ["Other"]
    if len(cat_list) > 3: cat_list = cat_list[:3]
    record["categories"] = cat_list
    record["category"] = ", ".join(cat_list)

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
    Return a normalized YouTube URL only when the original item is a video.

    Conditions:
        - original['type'] == 'video'
        - original['news_url'] matches YouTube domains

    Args:
        original: Original API item dict.

    Returns:
        str:
            Normalized YouTube URL for embedding, or '' if not applicable.
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
    """
    Mark a cryptonewsapi row as processed by its news_url.

    Args:
        news_url: Original news_url key for the row.

    Returns:
        None
    """
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE cryptonewsapi SET processed = 1 WHERE news_url = %s",
        (news_url,)
    )
    conn.commit()
    cur.close(); conn.close()

def validate_rewritten_article(doc: dict, raw: dict) -> None:
    """
    Ensure a generated article has the fields required before it is stored.

    Args:
        doc: Rewritten article JSON from OpenAI.
        raw: Source DB row used only for diagnostic context.

    Raises:
        ValueError:
            If required fields are missing or empty.
    """
    required = (
        "title",
        "full_text",
        "category",
        "hashtags",
        "sentiment",
        "seo_focus",
        "seo_slug",
        "seo_meta",
        "image_alt",
    )
    missing = [key for key in required if doc.get(key) in (None, "")]
    if missing:
        raise ValueError(
            "Generated article missing required fields "
            f"{missing}; article_id={raw.get('id')} title={raw.get('title')!r}"
        )
    if not str(doc.get("full_text", "")).strip():
        raise ValueError(
            "Generated article has empty full_text; "
            f"article_id={raw.get('id')} title={raw.get('title')!r}"
        )

def process_one(raw):
    """
    Process a single raw cryptonewsapi row through the GPT enrichment pipeline.

    Steps:
        - Optionally enrich via search (enrich_with_search)
        - Detect/normalize YouTube video URL (_maybe_video_url)
        - Rewrite/classify to strict JSON (classify_and_rewrite)
        - Repair if needed (repair_if_needed)
        - Add schema_jsonld if missing (build_news_schema_jsonld)
        - Ensure video URL is embedded if applicable
        - Store into rich_crpytonews (store_rich_news)
        - Mark original row processed (mark_processed)

    Args:
        raw: DB row dict from cryptonewsapi (expects at least title/text/news_url/type fields).

    Returns:
        bool:
            True when the article was stored and marked processed, False otherwise.
    """
    try:
        extra = enrich_with_search(raw)
        video_url = _maybe_video_url(raw)
        draft = classify_and_rewrite(raw, extra, video_url)
        final_doc = repair_if_needed(raw, extra, draft)

        if not final_doc.get("schema_jsonld"):
            sj = build_news_schema_jsonld(final_doc, raw)
            if sj:
                final_doc["schema_jsonld"] = sj

        if video_url and video_url not in final_doc.get("full_text", ""):
            final_doc["full_text"] = re.sub(r"</p>", f"</p>\n<p>{video_url}</p>", final_doc["full_text"], count=1, flags=re.I)

        validate_rewritten_article(final_doc, raw)
        logging.info("SEO dump: %s", {k: final_doc[k] for k in ("seo_focus","seo_slug","seo_meta")})
        store_rich_news(final_doc, raw)     # ✅ store first
        mark_processed(raw["news_url"])     # ✅ then mark processed
        return True
    except Exception:
        logging.exception(
            "GPT pipeline failed; article remains retryable. article_id=%s title=%r source=%r",
            raw.get("id"),
            raw.get("title"),
            raw.get("source_name"),
        )
        return False





def process_news_with_gpt(batch_size: int | None = None):
    """
    Process a batch of due articles from cryptonewsapi into rich_crpytonews.

    Batch sizing:
        - If batch_size is None, derives it from PROCESS_LOOKAHEAD_MINUTES,
          clamped to [PROCESS_BATCH_MIN..PROCESS_BATCH_MAX].

    Selection:
        - processed = 0
        - chosen_for_publish = 1
        - when batch_size is auto-derived, scheduled within PROCESS_LOOKAHEAD_MINUTES
        - ordered by scheduled_for ASC, selected_at ASC
        - limited to batch_size

    For each selected row, calls process_one().

    Args:
        batch_size: Optional explicit batch size override.

    Returns:
        dict:
            Processing counters with attempted, succeeded, and failed counts.
    """
    
    use_lookahead_filter = batch_size is None
    if use_lookahead_filter:
        batch_size = max(
            PROCESS_BATCH_MIN,
            min(PROCESS_BATCH_MAX, _count_due_within(PROCESS_LOOKAHEAD_MINUTES)),
        )

    fresh_start_clause = ""
    lookahead_clause = ""
    params: list[Any] = []
    if PIPELINE_FRESH_START_AFTER_UTC_SQL:
        logging.info(
            "Pipeline fresh-start cutoff active for processing: cryptonewsapi.insertDate >= %s",
            PIPELINE_FRESH_START_AFTER_UTC_SQL,
        )
        fresh_start_clause = " AND insertDate >= %s"
        params.append(PIPELINE_FRESH_START_AFTER_UTC_SQL)
    if use_lookahead_filter:
        lookahead_clause = """
          AND scheduled_for IS NOT NULL
          AND scheduled_for <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE)
        """
        params.append(PROCESS_LOOKAHEAD_MINUTES)
    params.append(batch_size)

    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute(f"""
        SELECT *
        FROM cryptonewsapi
        WHERE processed = 0
          AND chosen_for_publish = 1
          {fresh_start_clause}
          {lookahead_clause}
        ORDER BY scheduled_for ASC, selected_at ASC
        LIMIT %s
    """, tuple(params))
    articles = cur.fetchall()
    cur.close(); conn.close()

    attempted = len(articles)
    succeeded = 0
    failed = 0

    for art in articles:
        if process_one(art):
            succeeded += 1
        else:
            failed += 1

    result = {"attempted": attempted, "succeeded": succeeded, "failed": failed}
    if failed:
        logging.warning("GPT processing completed with failures: %s", result)
    else:
        logging.info("GPT processing completed: %s", result)
    return result



# ---- manual run -------------------------------------------------------------
if __name__ == "__main__":
    process_news_with_gpt()
