# news_fetcher.py
from __future__ import annotations

import os, json, time, math, hashlib, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Tuple
import threading
import requests
from email.utils import parsedate_to_datetime
import logging 
from apscheduler.schedulers.background import BackgroundScheduler
from decimal import Decimal
from db import get_db_connection
from config import CRYPTO_NEWS_TOKEN, OPENAI_API_KEY

# ========================= Tunables ==========================================

# How many to publish per local day
DAILY_TARGET = int(os.getenv("DAILY_TARGET", "25"))
ACTIVE_SHARE = float(os.getenv("ACTIVE_SHARE", "0.8"))   # 80% during active
ACTIVE_START_UTC = os.getenv("ACTIVE_START_UTC", "14:00")
ACTIVE_END_UTC   = os.getenv("ACTIVE_END_UTC",   "02:00")

# Only schedule items whose publish_date is reasonably fresh
SCHEDULE_MAX_AGE_HOURS = int(os.getenv("SCHEDULE_MAX_AGE_HOURS", "36"))



RUN_EVERY_MINUTES = 30                 # “half an hour”
TARGET_MIN = 15
COMPARISON_LOOKBACK_HOURS = 4
TARGET_MAX = 18
BREAKING_RESERVE = 3                   # up to +3 if breaking
BACKLOG_TTL_HOURS = 36                 # keep candidates fresh up to 36h

POOL_SIZE         = 10         # only keep this many candidates per pull
PROCESS_PER_CYCLE = 3    
ALLOW_VIDEO = False
# Pull knobs
ITEMS_PER_PULL = 100                   # API max
RANK_DAYS = 1                          # importance last 1 day

# Scoring weights (sum to ~1.0)
W_GPT       = 0.45
W_RANK      = 0.25
W_RECENCY   = 0.18
W_SOURCE    = 0.09
W_TICKERS   = 0.05

# Breaking thresholds
THRESH_BREAKING_NOW = 0.80             # publish immediately if >= and breaking
THRESH_PUBLISH      = 0.60             # otherwise queue/publish if we need to hit quota

# Your local timezone for daily counters (Belgrade)
LOCAL_TZ = timezone(timedelta(hours=2))  # CEST (+02) early Sep; adjust if needed

# Sources: simple prior weights (0..1)
SOURCE_WEIGHTS = {
    "Coindesk":       1.00,
    "Cointelegraph":  0.95,
    "The Block":      0.92,
    "Bloomberg":      0.90,
    "Reuters":        0.90,
    "Decrypt":        0.88,
    "BeInCrypto":     0.80,
    "Cryptopolitan":  0.75,
    "Crypto news":    0.70,
}

# Topics/tickers weights (very light nudge)
TICKER_WEIGHTS = {"BTC": 1.0, "ETH": 0.9, "SOL": 0.8, "XRP": 0.75, "BNB": 0.7}
TOP_MULTI_TICKERS = "BTC,ETH,SOL,XRP,BNB"

# ============================================================================

_session = requests.Session()
_session.headers.update({"User-Agent": "CryptoCourierFetcher/1.0"})
_api = "https://cryptonews-api.com/api/v1"




from zoneinfo import ZoneInfo  # py3.9+: for safety if you want real TZ math

def _parse_hhmm(s: str) -> Tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)

def _today_utc_bounds() -> Tuple[datetime, datetime]:
    # UTC day bounds
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end   = start + timedelta(days=1)
    return start, end

def _window_utc_for_today() -> Tuple[datetime, datetime]:
    """Return (active_start_utc, active_end_utc) for 'today', allowing wrap past midnight."""
    day_start, _ = _today_utc_bounds()
    h1, m1 = _parse_hhmm(ACTIVE_START_UTC)
    h2, m2 = _parse_hhmm(ACTIVE_END_UTC)

    active_start = day_start.replace(hour=h1, minute=m1)
    active_end   = day_start.replace(hour=h2, minute=m2)
    if active_end <= active_start:
        active_end += timedelta(days=1)  # wraps past midnight
    return active_start, active_end

def _generate_even_slots(start: datetime, end: datetime, count: int) -> List[datetime]:
    if count <= 0:
        return []
    total = (end - start).total_seconds()
    step  = total / count
    return [ (start + timedelta(seconds=round(i * step))).replace(microsecond=0) for i in range(count) ]


def _clean_url(url: str) -> str:
    """
    Strip common tracking params & normalize minor variants for dedupe.
    """
    if not url: return url
    # Remove UTM & common trackers
    url = re.sub(r'(\?|&)(utm_[^=]+|ref|src|feature|igshid|s|fbclid)=[^&#]*', r'', url, flags=re.I)
    # Remove dangling ? or &
    url = re.sub(r'[?&]+$', '', url)
    # Collapse multiple slashes (not after protocol)
    url = re.sub(r'(?<!:)//+', '/', url)
    # Trim trailing slash
    if url.endswith('/'): url = url[:-1]
    return url

def _hash_title(title: str) -> str:
    return hashlib.sha256((title or "").strip().lower().encode("utf-8")).hexdigest()

def _preblend_score(it: Dict[str, Any], now_utc: datetime) -> float:
    # use only API-side signals (no GPT yet)
    try:
        pub = _parse_et_date(it.get("date", "")) if it.get("date") else now_utc
    except Exception:
        pub = now_utc
    rec  = _recency_score(pub, now_utc)
    src  = _source_weight(it.get("source_name", ""))
    tick = _ticker_weight([t.strip() for t in (", ".join(it.get("tickers",[]) or [])).split(",") if t.strip()])
    rnk  = _norm_rank(it.get("rank_score"))
    # weights tuned to be decisive but simple
    return (0.45 * rnk) + (0.30 * rec) + (0.15 * src) + (0.10 * tick)


def _parse_et_date(s: str) -> datetime:
    """
    API dates are ET (e.g., 'Fri, 05 Sep 2025 09:19:41 -0400').
    We convert to UTC for storage/recency calc.
    """
    dt = parsedate_to_datetime(s)  # tz-aware
    return dt.astimezone(timezone.utc)

def _sentiment_num(val) -> float | None:
    if isinstance(val, (int, float)): return float(val)
    m = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}
    try:    return m.get(str(val).strip().lower(), 0.0)  # fallback 0.0
    except: return 0.0


def _recency_score(published_utc: datetime, now_utc: datetime) -> float:
    """
    Exponential decay, half-life ~6h. 1.0 when just published.
    """
    delta = max(0.0, (now_utc - published_utc).total_seconds() / 3600.0)
    half_life = 6.0
    return math.exp(-math.log(2) * (delta / half_life))

def _source_weight(name: str) -> float:
    return SOURCE_WEIGHTS.get(name, 0.6)

def _ticker_weight(tickers: List[str]) -> float:
    if not tickers: return 0.5
    return max([TICKER_WEIGHTS.get(t, 0.5) for t in tickers])

def _norm_rank(rank_score: str | float | None) -> float:
    """
    API rank_score is a string like '6.83' where higher = more important.
    We map roughly [0..10] → [0..1]. If missing, return 0.5 baseline.
    """
    if rank_score in (None, ""): return 0.5
    try:
        v = float(rank_score)
        return max(0.0, min(1.0, v / 10.0))
    except Exception:
        return 0.5

def _sentiment_to_float(val) -> float | None:
    if val is None or val == "": 
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    if s.startswith("pos"): return 1.0
    if s.startswith("neg"): return -1.0
    if s.startswith("neu"): return 0.0
    try:
        return float(s)
    except:
        return None


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



def _fetch(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    params = dict(params)
    params["token"] = CRYPTO_NEWS_TOKEN
    try:
        r = _session.get(endpoint, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        print(f"[FETCH] {endpoint} → HTTP {r.status_code}: {r.text[:200]}")
        return {}
    except Exception as e:
        print(f"[FETCH] Error: {e}")
        return {}

def _pull_batch() -> List[Dict[str, Any]]:
    """
    Aggregate multiple pulls (ranked, general, multi-ticker).
    Items are raw from API. Dedupe by news_id/url/title.
    """
    pulls: List[Dict[str, Any]] = []

    # 1) Rank-sorted (best signal) — articles only, last RANK_DAYS
    pulls.append(_fetch(_api, {
        "items": ITEMS_PER_PULL,
        "sortby": "rank",
        "days": RANK_DAYS,
        "type": "article",
        "tickers": TOP_MULTI_TICKERS,   # ← add this line
        "extra-fields": "id,eventid,rankscore",
    }))


    # 2) General category (wide coverage)
    pulls.append(_fetch(f"{_api}/category", {
        "section": "general",
        "items": ITEMS_PER_PULL,
        "page": 1
    }))

    # 3) Multi-ticker OR search (broad)
    pulls.append(_fetch(_api, {
        "tickers": TOP_MULTI_TICKERS,
        "items": ITEMS_PER_PULL,
        "page": 1,
        "extra-fields": "id,eventid,rankscore"
    }))

    # (Optional) videos: only if you choose to allow them
    if ALLOW_VIDEO:
        pulls.append(_fetch(_api, {
            "tickers": "BTC",
            "items": 50,
            "page": 1,
            "type": "video",
            "sortby": "rank",
            "days": RANK_DAYS,
            "extra-fields": "id,eventid,rankscore"
        }))

    # Flatten & dedupe
    seen_ids = set()
    seen_urls = set()
    seen_titles = set()
    out: List[Dict[str, Any]] = []

    for p in pulls:
        for it in p.get("data", []):
            news_id = it.get("news_id")
            url = _clean_url(it.get("news_url", ""))
            title = (it.get("title") or "").strip()
            thash = _hash_title(title)

            # basic dedupe
            if news_id and news_id in seen_ids:    continue
            if url and url in seen_urls:           continue
            if thash in seen_titles:               continue

            seen_ids.add(news_id)
            seen_urls.add(url)
            seen_titles.add(thash)

            it["_canonical_url"] = url
            it["_title_hash"] = thash
            out.append(it)
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    out.sort(key=lambda it: _preblend_score(it, now_utc), reverse=True)
    return out[:POOL_SIZE]

def _openai_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    # if using GPT-5 in Chat Completions, set minimal effort for speed
    if payload.get("model", "").startswith("gpt-5-mini"):
        payload.setdefault("reasoning_effort", "minimal")
        payload.setdefault("temperature", 1)

    r = _session.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()
def _as_str_list(value) -> str:
    """Accept list[str] or str or None and return a clean comma-separated string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join([str(x).strip() for x in value if str(x).strip()])
    # already a string from DB
    return str(value).strip()

def _to_json_scalar(v):
    """Make anything json.dumps-safe and sensible for the prompt."""
    if v is None:
        return ""
    if isinstance(v, (int, float, bool, str)):
        return v
    if isinstance(v, Decimal):
        # keep numeric feel for rank_score etc.
        try:
            return float(v)
        except Exception:
            return str(v)
    return str(v)

def ai_score_batch(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Ask GPT-5 to rate importance (0..1) and flag breaking for each item.
    Returns {canonical_url: {"gpt_importance": float, "is_breaking": int}}
    """
    sys = (
        "You are an editorial rater for a crypto newsroom. "
        "Score each item’s newsworthiness 0..1 (two decimals). "
        "Mark is_breaking=1 only if urgent market-moving or major regulatory/security event likely to affect a broad audience now. "
        "Be strict; most items are not breaking."
    )

    examples = []
    for it in items[:20]:
        # normalize shapes and types
        rank_val = _to_json_scalar(it.get("rank_score", ""))
        topics_s = _as_str_list(it.get("topics"))
        tickers_s = _as_str_list(it.get("tickers"))
        text_s   = str(it.get("text") or "")[:600]

        examples.append({
            "title":  _to_json_scalar(it.get("title","")),
            "source": _to_json_scalar(it.get("source_name","")),
            "date":   _to_json_scalar(it.get("date","")),   # already preformatted string
            "rank_score": rank_val,
            "topics": topics_s,
            "tickers": tickers_s,
            "text":  text_s,
            "url":   _clean_url(it.get("_canonical_url") or it.get("canonical_url","")),
        })

    user = {
        "instruction": "Return JSON array with objects: {url, gpt_importance, is_breaking}. No commentary.",
        "items": examples
    }

    resp = _openai_chat({
        "model": "gpt-5-mini",
        "temperature": 1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": sys},
            # 👇 default=str is a last-ditch guard, after we already coerced most things
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, default=str)}
        ]
    })

    choice = resp["choices"][0]
    content = choice.get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(f"Scoring returned empty content: {resp}")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Bad JSON from scorer: {content[:300]}...") from e

    out: Dict[str, Dict[str, Any]] = {}
    rows = data if isinstance(data, list) else data.get("items", [])
    for row in rows:
        url = _clean_url((row.get("url") or "").strip())
        if not url:
            continue
        gi = row.get("gpt_importance", 0)
        try:
            gi = float(gi)
        except Exception:
            gi = 0.0
        out[url] = {
            "gpt_importance": gi,
            "is_breaking": 1 if str(row.get("is_breaking","0")).lower() in ("1","true") else 0
        }
    return out
# -------------------- Persistence --------------------------------------------
def _insert_or_update(items: List[Dict[str, Any]], batch_id: str):
    conn = get_db_connection()
    cur  = conn.cursor()

    sql = """
        INSERT INTO cryptonewsapi
        (news_url, canonical_url, title, full_text, publish_date,
        source_name, topics, sentiment, type, tickers, image_url,
        insertDate, processed,
        news_id, event_id, rank_score, title_hash, fetch_batch_id)
        VALUES
        (%s,%s,%s,%s,%s,
        %s,%s,%s,%s,%s,%s,
        %s,%s,
        %s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
        source_name = VALUES(source_name),
        title = VALUES(title),
        full_text = VALUES(full_text),
        image_url = VALUES(image_url),
        event_id = VALUES(event_id),
        rank_score = VALUES(rank_score),
        title_hash = VALUES(title_hash),
        fetch_batch_id = VALUES(fetch_batch_id);
        """

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for it in items:
        topics = ", ".join(it.get("topics",[]) or [])
        tickers = ", ".join(it.get("tickers",[]) or [])
        pub_utc = _parse_et_date(it["date"]) if it.get("date") else datetime.utcnow().replace(tzinfo=timezone.utc)



        cur.execute(sql, (
            it.get("news_url"), it.get("_canonical_url"),
            it.get("title"), it.get("text",""),
            pub_utc.strftime("%Y-%m-%d %H:%M:%S"),
            it.get("source_name",""), topics, _sentiment_num(it.get("sentiment")),
            it.get("type",""), tickers, it.get("image_url",""),
            now,
            0,                               # <— processed (bound)
            it.get("news_id"), it.get("eventid"),
            it.get("rank_score"), it.get("_title_hash"), batch_id
        ))

    conn.commit()
    cur.close(); conn.close()

def _update_scores(scored: Dict[str, Dict[str, Any]]):
    if not scored: return
    conn = get_db_connection(); cur = conn.cursor()
    sql = """
    UPDATE cryptonewsapi
       SET gpt_importance = %s,
           is_breaking = %s
     WHERE canonical_url = %s
    """
    for url, v in scored.items():
        cur.execute(sql, (v["gpt_importance"], v["is_breaking"], url))
    conn.commit(); cur.close(); conn.close()

def _update_blended_scores(now_utc: datetime):
    """
    Compute recency/source/ticker components and final_importance for items within TTL.
    """
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
      SELECT id, canonical_url, publish_date, source_name, tickers, rank_score, gpt_importance
      FROM cryptonewsapi
      WHERE (publish_date >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s HOUR))
        AND (chosen_for_publish = 0)
    """, (BACKLOG_TTL_HOURS,))
    rows = cur.fetchall()

    upd = conn.cursor()
    sql = """
    UPDATE cryptonewsapi
       SET recency_score = %s,
           source_weight = %s,
           final_importance = %s
     WHERE id = %s
    """

    for r in rows:
        pub_utc = r["publish_date"].replace(tzinfo=timezone.utc) if isinstance(r["publish_date"], datetime) else datetime.strptime(str(r["publish_date"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        recency = _recency_score(pub_utc, now_utc)
        source = _source_weight(r["source_name"] or "")
        tickers = [t.strip() for t in (r.get("tickers") or "").split(",") if t.strip()]
        t_weight = _ticker_weight(tickers)
        rank_n = _norm_rank(r.get("rank_score"))
        gpt = float(r.get("gpt_importance") or 0.0)

        final = (W_GPT*gpt) + (W_RANK*rank_n) + (W_RECENCY*recency) + (W_SOURCE*source) + (W_TICKERS*t_weight)
        final = max(0.0, min(1.0, final))

        upd.execute(sql, (round(recency,3), round(source,3), round(final,3), r["id"]))

    conn.commit()
    upd.close(); cur.close(); conn.close()

# -------------------- Selection & quota --------------------------------------
def _today_bounds_local() -> Tuple[datetime, datetime]:
    now_local = datetime.now(tz=LOCAL_TZ)
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    return (start.astimezone(timezone.utc), end.astimezone(timezone.utc))

def _today_published_counts() -> Tuple[int, int]:
    """
    returns (published_total, published_breaking) for the current UTC day
    """
    start_utc, end_utc = _today_utc_bounds()  # ← use UTC bounds
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
      SELECT
        SUM(chosen_for_publish = 1 AND (selected_at BETWEEN %s AND %s)) AS chosen_today,
        SUM(is_breaking = 1 AND chosen_for_publish = 1 AND (selected_at BETWEEN %s AND %s)) AS breaking_today
      FROM cryptonewsapi
    """, (start_utc, end_utc, start_utc, end_utc))
    row = cur.fetchone() or (0,0)
    cur.close(); conn.close()
    return int(row[0] or 0), int(row[1] or 0)


def _plan_today_schedule():
    """
    Fill today's schedule by assigning scheduled_for times to unscheduled items.
    - Breaking (is_breaking=1 and final_importance >= THRESH_BREAKING_NOW) → schedule immediately (staggered).
    - Non-breaking → fill today's UTC active slots (80%) and off-peak slots (20%), up to remaining daily quota.
    """
    # 1) Breaking now (staggered times so all get a slot)
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id
        FROM cryptonewsapi
        WHERE chosen_for_publish = 0
          AND is_breaking = 1
          AND final_importance >= %s
          AND publish_date >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s HOUR)
        ORDER BY final_importance DESC, publish_date DESC
    """, (THRESH_BREAKING_NOW, COMPARISON_LOOKBACK_HOURS))
    breaking_ids = [r["id"] for r in cur.fetchall()]
    cur.close(); conn.close()
    if breaking_ids:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        times = [(now + timedelta(seconds=5*i)).replace(microsecond=0) for i in range(len(breaking_ids))]
        _assign_schedule(breaking_ids, times)

    # 2) Respect remaining daily capacity (UTC day)
    total_today, _ = _today_published_counts()  # make sure this counts the UTC day
    remaining_today = max(0, DAILY_TARGET - total_today)
    if remaining_today == 0:
        return

    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

    # 3) Build today's UTC windows (clamp active slice to *today only*)
    active_start, active_end = _window_utc_for_today()   # may cross midnight
    day_start, day_end       = _today_utc_bounds()       # today's UTC bounds

    active_a = max(active_start, day_start)
    active_b = min(active_end,   day_end)

    # Off-peak is the complement of the clamped active slice within today
    off_ranges: list[tuple[datetime, datetime]] = []
    if active_a > day_start:
        off_ranges.append((day_start, active_a))
    if active_b < day_end:
        off_ranges.append((active_b, day_end))

    # 4) Split remaining capacity 80/20 for today
    active_quota = int(round(remaining_today * ACTIVE_SHARE))
    off_quota    = max(0, remaining_today - active_quota)

    # Generate evenly-spaced slots
    active_slots = _generate_even_slots(active_a, active_b, active_quota) if active_b > active_a else []

    off_slots: list[datetime] = []
    if off_quota > 0 and off_ranges:
        total_off_secs = sum((b - a).total_seconds() for a, b in off_ranges if b > a)
        if total_off_secs > 0:
            step = total_off_secs / off_quota
            t = 0.0
            for _ in range(off_quota):
                target = t
                acc = 0.0
                for a, b in off_ranges:
                    span = (b - a).total_seconds()
                    if span <= 0:
                        continue
                    if acc + span >= target:
                        off_slots.append((a + timedelta(seconds=round(target - acc))).replace(microsecond=0))
                        break
                    acc += span
                t += step

    # Don’t schedule in the past (small cushion)
    cutoff = now_utc + timedelta(seconds=2)
    active_slots = [s for s in active_slots if s >= cutoff]
    off_slots    = [s for s in off_slots    if s >= cutoff]

    slots = [("active", ts) for ts in active_slots] + [("off", ts) for ts in off_slots]
    slots.sort(key=lambda x: x[1])
    if not slots:
        return

    # 5) Pick freshest viable candidates and assign to the slots
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id
        FROM cryptonewsapi
        WHERE chosen_for_publish = 0
          AND (is_breaking = 0 OR is_breaking IS NULL)
          AND final_importance >= %s
          AND publish_date >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s HOUR)
        ORDER BY final_importance DESC, publish_date DESC
        LIMIT %s
    """, (THRESH_PUBLISH, SCHEDULE_MAX_AGE_HOURS, len(slots)*2))  # overfetch a bit
    cand_ids = [r["id"] for r in cur.fetchall()]
    cur.close(); conn.close()
    if not cand_ids:
        return

    assign_ids   = cand_ids[:len(slots)]
    assign_times = [ts for _, ts in slots[:len(assign_ids)]]
    _assign_schedule(assign_ids, assign_times)



def _assign_schedule(ids: List[int], times: List[datetime]):
    """
    Assign scheduled_for to the given ids in order.
    Marks chosen_for_publish=1 and sets selected_at=now.
    """
    if not ids or not times:
        return
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection(); cur = conn.cursor()
    for i, post_id in enumerate(ids[:len(times)]):
        ts = times[i].strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            UPDATE cryptonewsapi
               SET chosen_for_publish = 1,
                   selected_at = %s,
                   scheduled_for = %s
             WHERE id = %s
        """, (now, ts, post_id))
    conn.commit(); cur.close(); conn.close()


def _select_top_k_for_batch(batch_id: str, k: int = PROCESS_PER_CYCLE):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id
        FROM cryptonewsapi
        WHERE fetch_batch_id = %s
        ORDER BY is_breaking DESC, final_importance DESC, publish_date DESC
        LIMIT %s
    """, (batch_id, k))
    ids = [r["id"] for r in cur.fetchall()]
    cur.close(); conn.close()
    if ids:
        logging.info("Chosen this batch: %s", ids)
        _mark_chosen(ids, False)


def _mark_chosen(ids: List[int], breaking: bool):
    if not ids: return
    conn = get_db_connection(); cur = conn.cursor()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    q = f"""
      UPDATE cryptonewsapi
         SET chosen_for_publish = 1,
             selected_at = %s
       WHERE id IN ({",".join(["%s"]*len(ids))})
    """
    cur.execute(q, (now, *ids))
    conn.commit(); cur.close(); conn.close()

# -------------------- Main cycle ---------------------------------------------
def run_fetch_cycle():
    conn = None
    conn2 = None
    lock_acquired = False
    try:
        conn = get_db_connection()
        if not _get_lock(conn, "news_fetcher_lock", 1):
            # another instance is already running
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            logging.info("[FETCH] Another fetcher instance is running; skipping.")
            return
        lock_acquired = True

        batch_id = hashlib.sha1(os.urandom(8)).hexdigest()[:22]
        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

        raw_items = _pull_batch()
        if not raw_items:
            logging.info("[FETCH] No items pulled")
            return

        _insert_or_update(raw_items, batch_id)

        # --- scoring prep on a separate cursor/conn ---
        conn2 = get_db_connection()
        cur2 = conn2.cursor(dictionary=True)
        cur2.execute("""
            SELECT canonical_url, title, source_name, publish_date,
                   rank_score, topics, tickers, full_text AS text
            FROM cryptonewsapi
            WHERE fetch_batch_id = %s
            ORDER BY publish_date DESC
            LIMIT %s
        """, (batch_id, min(20, POOL_SIZE)))
        rows = cur2.fetchall()
        cur2.close()
        conn2.close(); conn2 = None

        to_score = []
        for r in rows:
            pub = r["publish_date"]
            if isinstance(pub, str):
                pub = datetime.strptime(pub, "%Y-%m-%d %H:%M:%S")
            pub = pub.replace(tzinfo=timezone.utc)
            r["date"] = pub.strftime("%a, %d %b %Y %H:%M:%S -0000")
            to_score.append(r)

        logging.info("to_score rows for batch %s: %d", batch_id, len(to_score))

        scored = ai_score_batch(to_score)
        _update_scores(scored)
        _update_blended_scores(now_utc)

        _plan_today_schedule()  # your new scheduler/selection function
        tot, brk = _today_published_counts()
        logging.info("Chosen today so far: total=%d (breaking=%d)", tot, brk)

        logging.info("[FETCH] Cycle complete. Pulled=%d Scored=%d Batch=%s",
                     len(raw_items), len(scored), batch_id)

    except Exception:
        logging.exception("[FETCH] Cycle error")

    finally:
        # close secondary conn if something failed before we closed it
        try:
            if conn2 and getattr(conn2, "is_connected", lambda: False)():
                conn2.close()
        except Exception:
            pass

        # only try to RELEASE_LOCK if we actually hold it and the connection is alive
        try:
            if lock_acquired and conn and getattr(conn, "is_connected", lambda: False)():
                _release_lock(conn, "news_fetcher_lock")
        except Exception as e:
            logging.warning("Lock release skipped: %s", e)

        # always try to close the primary connection
        try:
            if conn:
                conn.close()
        except Exception:
            pass

_scheduler = None
_scheduler_lock = threading.Lock()

def start_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(daemon=True)
            _scheduler.add_job(
                run_fetch_cycle,
                "interval",
                minutes=RUN_EVERY_MINUTES,
                next_run_time=datetime.now(),   # ← run immediately once
                misfire_grace_time=60,
                max_instances=1,   # <- don’t overlap
                coalesce=True,     # <- collapse missed runs into one
                jitter=5   
            )
            _scheduler.start()
            print("[FETCH] Scheduler started.")

def fetch_all_news():
    """Compatibility wrapper for older scheduler. Runs one full fetch cycle now."""
    run_fetch_cycle()