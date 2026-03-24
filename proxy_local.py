#!/usr/bin/env python3
"""
WB Election Intel — Local LLM Proxy
=====================================
Supports:
  - Ollama        (local, free, offline-capable)
  - Groq          (cloud, free tier, fast)
  - Google Gemini (cloud, free tier, has web search)
  - OpenAI-compat (any OpenAI-compatible endpoint)

Also includes a built-in RSS news fetcher that pre-loads
Indian election news headlines into the prompt, replacing
Claude's web_search tool.

Usage:
  python3 proxy_local.py --backend ollama
  python3 proxy_local.py --backend groq    --key gsk_YOUR_KEY
  python3 proxy_local.py --backend gemini  --key AIza_YOUR_KEY
  python3 proxy_local.py --backend openai  --key sk-... --url https://api.openai.com

Then open: http://localhost:5050
"""

import sys, os, json, re, argparse, threading, time, socket, socketserver, urllib.request, urllib.error
import webbrowser, xml.etree.ElementTree as ET, sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR     = Path(__file__).parent.resolve()
DASHBOARD_FILE = SCRIPT_DIR / "wb_live_intel_dashboard.html"
OSINT_FILE     = SCRIPT_DIR / "wb_osint_monitor.html"
DEFAULT_PORT   = 5055   # dev sandbox — prod runs on 5050

# ─── PHASE 2 FEATURE FLAGS ────────────────────────────────────────────────────
# Set to False to instantly disable without touching any other code.
FEATURE_SQLITE   = True   # persist cycles + articles to bankura_intel.db
FEATURE_DIGEST   = True   # background LLM digest of MED/HIGH articles

_wire_cache = {"ts": 0.0, "items": []}   # ANI+PTI wire feed cache (10-min TTL)

DB_PATH  = SCRIPT_DIR / "bankura_intel.db"
_db_lock = threading.Lock()   # single write-lock for all DB operations

# ─── SQLite helpers ───────────────────────────────────────────────────────────

def _db_conn():
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15)
    c.row_factory = sqlite3.Row
    return c

def _db_init():
    """Create tables on first run. Safe to call repeatedly (IF NOT EXISTS)."""
    if not FEATURE_SQLITE:
        return
    with _db_lock:
        c = _db_conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS digested_articles (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url           TEXT    UNIQUE,
                title         TEXT,
                source        TEXT,
                severity      TEXT,
                panel         TEXT,
                published_at  TEXT,
                first_seen    TEXT,
                full_text     TEXT,
                digest        TEXT,
                digest_status TEXT DEFAULT 'pending',
                digested_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_da_status   ON digested_articles(digest_status);
            CREATE INDEX IF NOT EXISTS idx_da_severity ON digested_articles(severity);
            CREATE INDEX IF NOT EXISTS idx_da_seen     ON digested_articles(first_seen);
            CREATE TABLE IF NOT EXISTS cycles (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at       TEXT,
                threat_level     TEXT,
                total_signals    INTEGER,
                high_alerts      INTEGER,
                violence_reports INTEGER,
                full_json        TEXT
            );
            CREATE TABLE IF NOT EXISTS turnout_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at     TEXT,
                district        TEXT,
                turnout_pct     REAL,
                booths_total    INTEGER,
                booths_reported INTEGER,
                source          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ts_district
                ON turnout_snapshots(district, recorded_at);

            -- Phase 4B: Daily Briefing
            CREATE TABLE IF NOT EXISTS briefing_config (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS briefing_sends (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at      TEXT,
                trigger      TEXT,
                threat_level TEXT,
                headline     TEXT,
                ntfy_status  TEXT,
                sms_status   TEXT,
                cycle_id     INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_bsends_sent
                ON briefing_sends(sent_at);
        """)
        c.commit(); c.close()
    print(f"  [DB] Initialised: {DB_PATH}")

def _save_digest_candidates(panel_items):
    """
    After each fetch, queue new MED/HIGH articles for background digesting.
    Dedup by URL — INSERT OR IGNORE means same article from multiple feeds
    is stored only once.
    """
    if not FEATURE_SQLITE or not FEATURE_DIGEST:
        return
    now = datetime.now(timezone.utc).isoformat()
    _DIGEST_WB_KW = [
        "west bengal", "bengal", "bankura", "bishnupur", "kolkata", "calcutta",
        "mamata", "trinamool", "tmc", "wb election", "wb 2026", "wb poll",
        "purulia", "jhargram", "birbhum", "bardhaman", "medinipur", "howrah",
        "saltora", "chhatna", "ranibandh", "taldangra", "barjora", "sonamukhi",
    ]
    # Panel priority: what the observer cares about most, first
    panel_order = ["bankura","alerts","surrounds","statewide","official","parties","bangla"]
    candidates = []
    seen_urls  = set()
    for panel in panel_order:
        for item in panel_items.get(panel, []):
            url = (item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            # Drop YouTube — video thumbnails have no digestible text.
            # Check both URL (direct links) and source field (Google News redirects
            # to YouTube return source="YouTube" in the RSS <source> element).
            src = item.get("source", "")
            if "youtube.com" in url or "youtu.be" in url or src == "YouTube":
                continue
            sev = _score_severity(item.get("title","") + " " + item.get("desc",""))
            if sev not in ("high", "medium"):
                continue
            # WB relevance gate — drop non-Bengal articles (UP, Bihar, national, etc.)
            text = (item.get("title","") + " " + item.get("desc","")).lower()
            if not any(kw in text for kw in _DIGEST_WB_KW):
                continue
            seen_urls.add(url)
            candidates.append((
                url,
                (item.get("title") or "")[:500],
                item.get("source",""),
                sev, panel,
                item.get("time",""), now
            ))
    if not candidates:
        return
    with _db_lock:
        c = _db_conn()
        inserted = 0
        for row in candidates:
            c.execute("""
                INSERT OR IGNORE INTO digested_articles
                  (url, title, source, severity, panel, published_at, first_seen, digest_status)
                VALUES (?,?,?,?,?,?,?,'pending')
            """, row)
            if c.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        c.commit(); c.close()
    if inserted:
        print(f"  [digest] +{inserted} article(s) queued")

def _fetch_article_text(url, timeout=8):
    """
    Try to GET full article HTML and extract paragraph text.
    Returns extracted text (up to 3000 chars) or None if blocked/paywalled.
    No external dependencies — pure stdlib.
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(200_000).decode("utf-8", errors="replace")
        paras = re.findall(r'<p[^>]*>\s*([^<]{60,})\s*</p>', raw, re.I)
        text  = " ".join(p.strip() for p in paras[:20])
        text  = re.sub(r'<[^>]+>', ' ', text)
        text  = re.sub(r'\s{2,}', ' ', text).strip()
        return text[:3000] if len(text) > 120 else None
    except Exception:
        return None

def _digest_one(row_dict, ollama_base, model):
    """Call Ollama to produce a 3-sentence digest. Returns string or None."""
    title    = row_dict.get("title","")
    body     = row_dict.get("full_text") or ""
    source   = row_dict.get("source","")
    panel    = row_dict.get("panel","")
    severity = (row_dict.get("severity") or "medium").upper()
    content  = body if len(body) > 200 else title
    prompt = (
        f"Election intelligence analyst, Bankura district observer, WB 2026.\n"
        f"Write a digest of this {severity}-priority article from '{source}' [{panel} feed].\n"
        f"Output exactly 3 numbered points, each a single sentence, no headings or labels:\n"
        f"1. What happened (facts only).\n"
        f"2. Who is involved and where.\n"
        f"3. Why it matters for Bankura Phase 1 (23 Apr 2026) observer.\n\n"
        f"Article: {content}\n\nDigest:"
    )
    try:
        payload = json.dumps({
            "model":    model,
            "messages": [{"role":"user","content":prompt}],
            "stream":   False,
            "options":  {"temperature":0.1,"num_ctx":2048,"think":False},
        }).encode()
        req = urllib.request.Request(
            f"{ollama_base}/api/chat",
            data=payload,
            headers={"Content-Type":"application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        msg = data.get("message") or {}
        return (msg.get("content") or "").strip() or None
    except Exception as e:
        print(f"  [digest] LLM error: {e}")
        return None

_digest_worker_active = False

def _start_digest_worker(ollama_base, model):
    """Launch the background digest thread (daemon — exits with main process)."""
    if not FEATURE_SQLITE or not FEATURE_DIGEST:
        return
    def _worker():
        global _digest_worker_active
        _digest_worker_active = True
        print("  [digest] Background worker started")
        while True:
            try:
                with _db_lock:
                    c = _db_conn()
                    row = c.execute("""
                        SELECT id, url, title, source, severity, panel, full_text
                        FROM digested_articles WHERE digest_status='pending'
                        ORDER BY CASE severity WHEN 'high' THEN 1 ELSE 2 END,
                                 CASE panel WHEN 'bankura' THEN 1 WHEN 'alerts' THEN 2
                                            WHEN 'surrounds' THEN 3 ELSE 4 END,
                                 first_seen ASC
                        LIMIT 1
                    """).fetchone()
                    c.close()

                if row is None:
                    time.sleep(300); continue   # idle: check every 5 min

                rid   = row["id"]
                title = row["title"]
                url   = row["url"]

                # Mark in-progress to prevent double processing
                with _db_lock:
                    c = _db_conn()
                    c.execute("UPDATE digested_articles SET digest_status='processing' WHERE id=?", (rid,))
                    c.commit(); c.close()

                print(f"  [digest] Fetching: {title[:70]}…")
                full_text = _fetch_article_text(url) or ""
                if full_text:
                    with _db_lock:
                        c = _db_conn()
                        c.execute("UPDATE digested_articles SET full_text=? WHERE id=?", (full_text, rid))
                        c.commit(); c.close()

                row_dict = dict(row); row_dict["full_text"] = full_text
                digest   = _digest_one(row_dict, ollama_base, model)
                now      = datetime.now(timezone.utc).isoformat()

                with _db_lock:
                    c = _db_conn()
                    if digest:
                        c.execute("""UPDATE digested_articles
                                     SET digest=?, digest_status='done', digested_at=?
                                     WHERE id=?""", (digest, now, rid))
                        print(f"  [digest] ✓ {title[:70]}")
                    else:
                        c.execute("UPDATE digested_articles SET digest_status='failed' WHERE id=?", (rid,))
                        print(f"  [digest] ✗ failed: {title[:70]}")
                    c.commit(); c.close()

                time.sleep(20)   # pace between articles — don't overload Ollama

            except Exception as e:
                print(f"  [digest] Worker error: {e}")
                time.sleep(60)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

# ── ANI + PTI wire fetcher (cached 10 min) ─────────────────────────────────

def _fetch_wire():
    """Fetch ANI + PTI Google News RSS for West Bengal, cache 10 min.
    Only articles published within the last 30 hours are included."""
    global _wire_cache
    if time.time() - _wire_cache["ts"] < 600:   # 10-min cache
        return _wire_cache["items"]

    import email.utils as _eu
    WIRE_MAX_AGE_H = 48          # 48h window
    now_ts = time.time()
    cutoff_ts = now_ts - WIRE_MAX_AGE_H * 3600

    items = []
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    for source, url in WIRE_FEEDS:
        try:
            req  = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read().lstrip(b'\xef\xbb\xbf')
            root    = ET.fromstring(data)
            entries = root.findall(".//item")
            seen    = set()
            for it in entries[:40]:          # scan more items since old ones get dropped
                title = (it.findtext("title") or "").strip()
                title = re.sub(r"\s*[-|]\s*[A-Z][^|]{2,35}$", "", title).strip()
                if not title or len(title) < 10:
                    continue
                fp = re.sub(r"\W+", "", title.lower())[:40]
                if fp in seen:
                    continue
                seen.add(fp)
                link    = (it.findtext("link") or "").strip()
                pub_raw = (it.findtext("pubDate") or "").strip()
                # ── Time filter: drop items older than 30 h ───────────────
                if pub_raw:
                    try:
                        pub_ts = _eu.parsedate_to_datetime(pub_raw).timestamp()
                        if pub_ts < cutoff_ts:
                            continue          # too old
                        pub_iso = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
                    except Exception:
                        pub_iso = pub_raw     # unparseable — keep item, show raw string
                else:
                    pub_iso = ""
                # ── WB relevance filter ───────────────────────────────────
                tl = title.lower()
                if not any(kw in tl for kw in _WIRE_WB_KW):
                    continue
                sev = _score_severity(title)
                items.append({
                    "source":   source,
                    "title":    title,
                    "url":      link,
                    "pub":      pub_iso,
                    "severity": sev,
                })
        except Exception as e:
            print(f"  [wire] {source} fetch error: {e}")

    # Sort: HIGH first, then newest first
    # pub is stored as ISO string — fromisoformat() handles it correctly
    def _pub_ts(item):
        try:
            return datetime.fromisoformat(item["pub"]).timestamp() if item["pub"] else 0
        except Exception:
            return 0
    items.sort(key=lambda x: (
        0 if x["severity"] == "high" else (1 if x["severity"] == "medium" else 2),
        -_pub_ts(x)
    ))
    _wire_cache = {"ts": time.time(), "items": items}
    return items

# ── Phase 3B: Fetch candidate news from Google News (grouped by AC) ──────────
def _fetch_figures_news():
    """Fetch Google News per AC, tag articles back to individual candidates.
    Returns dict: {candidate_name: [{"title","url","pub","source","ac"}]}
    Cached 15 min; 13 AC-grouped queries run concurrently via threads."""
    global _FIGURES_NEWS_CACHE
    if time.time() - _FIGURES_NEWS_CACHE["ts"] < 900:   # 15-min cache
        return _FIGURES_NEWS_CACHE["data"]

    result = {}   # name -> list of articles
    lock   = threading.Lock()

    def _fetch_ac(ac_label, names, url):
        try:
            req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            root = ET.fromstring(raw)
            items = root.findall(".//item")
            for it in items:
                title = (it.findtext("title") or "").strip()
                link  = it.findtext("link") or ""
                pub   = it.findtext("pubDate") or ""
                # Convert RFC2822 pubDate → ISO
                try:
                    import email.utils as _eu
                    pub = datetime.fromtimestamp(
                        _eu.parsedate_to_datetime(pub).timestamp(),
                        tz=timezone.utc
                    ).isoformat()
                except Exception:
                    pub = pub
                # Age filter — 60 days (candidate news cycles slower than wire)
                try:
                    from datetime import datetime as _dt
                    age_h = (datetime.now(timezone.utc) -
                             _dt.fromisoformat(pub)).total_seconds() / 3600
                    if age_h > 180 * 24:  # 4320h = 180 days (6 months)
                        continue
                except Exception:
                    pass
                # Tag to whichever candidate name appears in title (case-insensitive)
                title_l = title.lower()
                tagged  = False
                for name in names:
                    # Match on last name or full name
                    parts = [p.lower() for p in name.replace("Dr. ","").split() if len(p) > 3]
                    if any(p in title_l for p in parts):
                        article = {"title": title, "url": link, "pub": pub,
                                   "source": "Google News", "ac": ac_label}
                        with lock:
                            result.setdefault(name, []).append(article)
                        tagged = True
                # If no specific name match, attach to AC — only first candidate as sentinel
                # so the AC still shows up in the result even without named mentions
                if not tagged and len(title) > 10:
                    article = {"title": title, "url": link, "pub": pub,
                               "source": "Google News", "ac": ac_label}
                    with lock:
                        result.setdefault(names[0], []).append(article)
        except Exception as e:
            print(f"  [figures-news] {ac_label} error: {e}")

    threads = [threading.Thread(target=_fetch_ac, args=(ac, names, url), daemon=True)
               for ac, names, url in FIGURE_AC_FEEDS]
    for t in threads: t.start()
    for t in threads: t.join(timeout=20)

    # Keep only latest 3 articles per candidate, sorted newest first
    def _ts(a):
        try: return datetime.fromisoformat(a["pub"]).timestamp()
        except: return 0
    for name in result:
        result[name] = sorted(result[name], key=_ts, reverse=True)[:3]

    _FIGURES_NEWS_CACHE = {"ts": time.time(), "data": result}
    return result

# ── Political figures to monitor for feed mentions ───────────────────────────
# Updated March 2026 — all declared 2026 candidates for Bankura district ACs
POLITICAL_FIGURES = [
    # ── BJP 2026 CANDIDATES ──────────────────────────────────────────────────
    {"name": "Chandana Bauri",            "party": "BJP",  "role": "BJP candidate AC 247 Saltora (SC)",
     "keywords": ["chandana bauri", "chandana"]},
    {"name": "Satyanarayan Mukhopadhyay", "party": "BJP",  "role": "BJP candidate AC 248 Chhatna",
     "keywords": ["satyanarayan mukhopadhyay", "satyanarayan", "satynarayan mukhopadhyay"]},
    {"name": "Kshudiram Tudu",            "party": "BJP",  "role": "BJP candidate AC 249 Ranibandh (ST)",
     "keywords": ["kshudiram tudu", "khsudiram"]},
    {"name": "Kshetra Mohan Hansda",      "party": "BJP",  "role": "BJP candidate AC 250 Raipur (ST)",
     "keywords": ["kshetra mohan hansda", "kshetra hansda", "kshetra mohan"]},
    {"name": "Souvik Patra",              "party": "BJP",  "role": "BJP candidate AC 251 Taldangra",
     "keywords": ["souvik patra", "sauvik patra"]},
    {"name": "Niladri Sekhar Dana",       "party": "BJP",  "role": "BJP candidate AC 252 Bankura (Sadar)",
     "keywords": ["niladri sekhar dana", "niladri dana", "niladri sekhar"]},
    {"name": "Billeshwar Singha",         "party": "BJP",  "role": "BJP candidate AC 253 Barjora",
     "keywords": ["billeshwar singha", "billeshwar"]},
    {"name": "Amarnath Shakha",           "party": "BJP",  "role": "BJP candidate AC 254 Onda",
     "keywords": ["amarnath shakha", "amarnath"]},
    {"name": "Viswajit Khan",             "party": "BJP",  "role": "BJP candidate AC 255 Bishnupur (replaced Shukla Chatterjee)",
     "keywords": ["viswajit khan", "biswajit khan", "vishwajit khan"]},
    {"name": "Laxmikanta Majumdar",       "party": "BJP",  "role": "BJP candidate AC 256 Kotulpur (SC)",
     "keywords": ["laxmikanta majumdar", "lakshmikanta majumdar"]},
    {"name": "Nirmal Kumar Dhara",        "party": "BJP",  "role": "BJP candidate AC 257 Indas (SC)",
     "keywords": ["nirmal kumar dhara", "nirmal dhara"]},
    {"name": "Dibakar Gharami",           "party": "BJP",  "role": "BJP candidate AC 258 Sonamukhi (SC)",
     "keywords": ["dibakar gharami", "dibakar"]},
    # ── TMC 2026 CANDIDATES ──────────────────────────────────────────────────
    {"name": "Uttam Bauri",               "party": "TMC",  "role": "TMC candidate AC 247 Saltora (SC)",
     "keywords": ["uttam bauri"]},
    {"name": "Swapan Kumar Mandal",       "party": "TMC",  "role": "TMC candidate AC 248 Chhatna",
     "keywords": ["swapan kumar mandal", "swapan mandal"]},
    {"name": "Dr. Tanushree Hansda",      "party": "TMC",  "role": "TMC candidate AC 249 Ranibandh (ST)",
     "keywords": ["tanushree hansda", "tanushri hansda"]},
    {"name": "Thakur Moni Soren",         "party": "TMC",  "role": "TMC candidate AC 250 Raipur (ST)",
     "keywords": ["thakur moni soren", "thakurmoni soren"]},
    {"name": "Falguni Singhababu",        "party": "TMC",  "role": "TMC candidate AC 251 Taldangra",
     "keywords": ["falguni singhababu", "falguni"]},
    {"name": "Dr. Anup Mondal",           "party": "TMC",  "role": "TMC candidate AC 252 Bankura (Sadar)",
     "keywords": ["anup mondal", "arup mandal", "anup mandal"]},
    {"name": "Goutam Mishra",             "party": "TMC",  "role": "TMC candidate AC 253 Barjora",
     "keywords": ["goutam mishra", "goutam shyam", "shyam mishra"]},
    {"name": "Subrata Dutta",             "party": "TMC",  "role": "TMC candidate AC 254 Onda",
     "keywords": ["subrata dutta", "subrata datta gope", "subrata datta"]},
    {"name": "Tanmoy Ghosh",              "party": "TMC",  "role": "TMC candidate AC 255 Bishnupur",
     "keywords": ["tanmoy ghosh", "tanmay ghosh"]},
    {"name": "Harakali Pratihar",         "party": "TMC",  "role": "TMC candidate AC 256 Kotulpur (SC)",
     "keywords": ["harakali pratihar", "harakali"]},
    {"name": "Shyamali Roy Bagdi",        "party": "TMC",  "role": "TMC candidate AC 257 Indas (SC)",
     "keywords": ["shyamali roy bagdi", "shyamali roy", "shyamali bagdi"]},
    {"name": "Dr. Kallol Saha",           "party": "TMC",  "role": "TMC candidate AC 258 Sonamukhi (SC)",
     "keywords": ["kallol saha", "kallol"]},
    # ── CPI(M) / LEFT FRONT CANDIDATES ──────────────────────────────────────
    {"name": "Debalina Hembram",          "party": "CPIM", "role": "CPI(M) candidate AC 249 Ranibandh (ST)",
     "keywords": ["debalina hembram", "debalina"]},
    {"name": "Rajib Kar",                 "party": "CPIM", "role": "RSP candidate AC 248 Chhatna",
     "keywords": ["rajib kar"]},
    {"name": "Abhayananda Mukherjee",     "party": "CPIM", "role": "CPI(M) candidate AC 252 Bankura",
     "keywords": ["abhayananda mukherjee", "abhayananda"]},
    {"name": "Sujit Chakraborty",         "party": "CPIM", "role": "CPI(M) candidate AC 253 Barjora",
     "keywords": ["sujit chakraborty", "sujit chakra"]},
    {"name": "Ramchandra Roy",            "party": "CPIM", "role": "CPI(M) candidate AC 256 Kotulpur (SC)",
     "keywords": ["ramchandra roy"]},
    {"name": "Mona Mallick",              "party": "CPIM", "role": "CPI(M) candidate AC 257 Indas (SC)",
     "keywords": ["mona mallick", "mona mallik", "jharna mallick"]},
    {"name": "Ajit Roy",                  "party": "CPIM", "role": "CPI(M) candidate AC 258 Sonamukhi (SC)",
     "keywords": ["ajit roy"]},
    # ── KEY DISTRICT LEADERS (non-candidates but newsworthy) ─────────────────
    {"name": "Saumitra Khan",             "party": "BJP",  "role": "Bishnupur MP, BJP district face",
     "keywords": ["saumitra khan", "saumitra"]},
    {"name": "Arup Chakraborty",          "party": "TMC",  "role": "Bankura MP + District President",
     "keywords": ["arup chakraborty", "arup chakra"]},
    {"name": "Dr. Subhas Sarkar",         "party": "BJP",  "role": "Ex-MP Bankura, ex-Union Minister",
     "keywords": ["subhas sarkar"]},
    {"name": "Basudeb Acharia",           "party": "CPIM", "role": "Ex-9-term Bankura MP",
     "keywords": ["basudeb acharia", "basudeb"]},
]

# ── Sources — organised by dashboard panel ────────────────────────────────────
#
# SOCIAL MEDIA NOTES:
#   X/Twitter: Direct API requires paid access. We use:
#     1. Nitter RSS (open source Twitter frontend) — free, no auth
#     2. X embedded search links (passed to dashboard as clickable URLs)
#     3. Google News RSS filtered to Twitter/X content
#   Reddit: RSS feeds available for subreddits and searches (free, no auth)
#   Telegram: No public RSS; links passed as monitoring URLs
#   Facebook: No public RSS; monitoring via Google News
#
# TIME FILTERING:
#   MAX_AGE_HOURS_BANKURA  = 72h  (3 days — local Bankura items kept longer)
#   MAX_AGE_HOURS_GENERAL  = 36h  (statewide/surrounds)
#   MAX_AGE_HOURS_BREAKING = 24h  (alerts/official — only fresh content)
#   Items older than their panel threshold are silently dropped.

MAX_AGE_HOURS = {
    "bankura":   96,   # 4 days for local Bankura items
    "surrounds": 48,   # 48h for surrounding districts
    "statewide": 48,   # 48h for WB statewide
    "alerts":    48,   # 48h for alerts — pre-election coverage
    "official":  48,   # 48h for official updates
    "social":    48,   # 48h for social media signals
    "parties":   48,   # 48h for party statements/press releases
    "bangla":    48,   # 48h for Bengali-language sources
}

# NOTE: Nitter (open-source Twitter frontend with RSS) was removed.
# Nitter instances are frequently down, causing silent feed failures.
# Twitter/X content is now captured via Google News search queries instead,
# which aggregates viral tweets and news that cites Twitter sources reliably.

# Google News RSS — reliable, no auth, good coverage
def gnews(query):
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"

def gnews_bn(query):
    """Google News RSS — Bengali language edition (returns Bengali-language articles)."""
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=bn&gl=IN&ceid=IN:bn"

# ── Phase 3B: Candidate news feeds (grouped by AC, used by /api/figures-news) ─
# One Google News query per assembly constituency — candidates ORed together.
# 12 queries run concurrently, results tagged back to individual candidates.
FIGURE_AC_FEEDS = [
    # (ac_label, [candidate names], rss_url)
    ("AC247 Saltora",
     ["Chandana Bauri", "Uttam Bauri"],
     gnews('"Chandana Bauri" OR "Uttam Bauri" West Bengal election 2026')),
    ("AC248 Chhatna",
     ["Satyanarayan Mukhopadhyay", "Swapan Kumar Mandal", "Rajib Kar"],
     gnews('"Satyanarayan Mukhopadhyay" OR "Swapan Kumar Mandal" OR "Rajib Kar" West Bengal 2026')),
    ("AC249 Ranibandh",
     ["Kshudiram Tudu", "Dr. Tanushree Hansda", "Debalina Hembram"],
     gnews('"Kshudiram Tudu" OR "Tanushree Hansda" OR "Debalina Hembram" West Bengal election 2026')),
    ("AC250 Raipur",
     ["Kshetra Mohan Hansda", "Thakur Moni Soren"],
     gnews('"Kshetra Mohan Hansda" OR "Thakur Moni Soren" West Bengal election 2026')),
    ("AC251 Taldangra",
     ["Souvik Patra", "Falguni Singhababu"],
     gnews('"Souvik Patra" OR "Falguni Singhababu" West Bengal election 2026')),
    ("AC252 Bankura",
     ["Niladri Sekhar Dana", "Dr. Anup Mondal", "Abhayananda Mukherjee"],
     gnews('"Niladri Sekhar Dana" OR "Anup Mondal" OR "Abhayananda Mukherjee" Bankura election 2026')),
    ("AC253 Barjora",
     ["Billeshwar Singha", "Goutam Mishra", "Sujit Chakraborty"],
     gnews('"Billeshwar Singha" OR "Goutam Mishra" OR "Sujit Chakraborty" Barjora election 2026')),
    ("AC254 Onda",
     ["Amarnath Shakha", "Subrata Dutta"],
     gnews('"Amarnath Shakha" OR "Subrata Dutta" Onda West Bengal election 2026')),
    ("AC255 Bishnupur",
     ["Viswajit Khan", "Tanmoy Ghosh"],
     gnews('"Viswajit Khan" OR "Tanmoy Ghosh" Bishnupur election 2026')),
    ("AC256 Kotulpur",
     ["Laxmikanta Majumdar", "Harakali Pratihar", "Ramchandra Roy"],
     gnews('"Laxmikanta Majumdar" OR "Harakali Pratihar" OR "Ramchandra Roy" Kotulpur election 2026')),
    ("AC257 Indas",
     ["Nirmal Kumar Dhara", "Shyamali Roy Bagdi", "Mona Mallick"],
     gnews('"Nirmal Kumar Dhara" OR "Shyamali Roy Bagdi" OR "Mona Mallick" Indas election 2026')),
    ("AC258 Sonamukhi",
     ["Dibakar Gharami", "Dr. Kallol Saha", "Ajit Roy"],
     gnews('"Dibakar Gharami" OR "Kallol Saha" OR "Ajit Roy" Sonamukhi election 2026')),
    # Key district leaders — separate query
    ("Leaders",
     ["Saumitra Khan", "Arup Chakraborty", "Dr. Subhas Sarkar", "Basudeb Acharia"],
     gnews('"Saumitra Khan" OR "Subhas Sarkar" OR "Arup Chakraborty" OR "Basudeb Acharia" Bankura West Bengal 2026')),
]
_FIGURES_NEWS_CACHE = {"ts": 0.0, "data": {}}  # {candidate_name: [articles]}

# ── Wire agency feeds (WB-scoped, used by /api/wire) ─────────────────────────
# ANI: site:aninews.in returns Google-cached stale articles (100–40000h old)
#      Using keyword query instead — same approach as PTI
# IANS: dropped — Google News returns stub "IANS - IANS" titles only (unusable)
WIRE_FEEDS = [
    ("ANI", gnews("ANI \"West Bengal\" election 2026")),
    ("PTI", gnews("PTI \"West Bengal\" election 2026")),
]
# Minimum keywords one of which must appear in title for wire item to pass WB filter
_WIRE_WB_KW = [
    "west bengal", "bengal", "bankura", "kolkata", "calcutta", "mamata",
    "trinamool", "tmc", "bjp", "cpm", "wb election", "wb poll",
    "bishnupur", "purulia", "jhargram", "medinipur", "howrah",
]

RSS_FEEDS = [
    # ── BANKURA LOCAL (4 days) ────────────────────────────────────────────
    ("bankura", "Google: Bankura election 2026",
     gnews("Bankura election 2026")),
    ("bankura", "Google: Bankura candidate campaign",
     gnews("Bankura candidate campaign rally 2026")),
    ("bankura", "Google: Bishnupur election",
     gnews("Bishnupur election 2026")),
    ("bankura", "Google: Bishnupur Barjora Sonamukhi",
     gnews("Bishnupur OR Barjora OR Sonamukhi election 2026")),
    ("bankura", "Google: Saltora Chhatna Taldangra Onda",
     gnews("Saltora OR Chhatna OR Taldangra OR Onda election 2026")),
    ("bankura", "Google: Bankura violence MCC",
     gnews("Bankura election violence OR MCC violation 2026")),
    ("bankura", "Google: Kotulpur Indas Raipur Ranibandh",
     gnews("Kotulpur OR Indas OR Raipur OR Ranibandh election 2026")),
    ("bankura", "Google: Bankura polling booth security",
     gnews("Bankura polling booth security paramilitary 2026")),

    # ── SURROUNDING DISTRICTS (36h) ────────────────────────────────────────
    ("surrounds", "Google: Purulia election",
     gnews("Purulia election 2026")),
    ("surrounds", "Google: Jhargram election",
     gnews("Jhargram election 2026")),
    ("surrounds", "Google: Birbhum election",
     gnews("Birbhum election 2026")),
    ("surrounds", "Google: Paschim Bardhaman election",
     gnews("Paschim Bardhaman OR Asansol OR Durgapur election 2026")),
    ("surrounds", "Google: Paschim Medinipur election",
     gnews("Paschim Medinipur OR Kharagpur election 2026")),

    # ── WEST BENGAL STATEWIDE — election-specific only ─────────────────────
    ("statewide", "Google: WB election Phase 1 violence booth",
     gnews("West Bengal election Phase 1 2026 violence OR booth OR MCC OR candidate")),
    ("statewide", "Google: TMC BJP clash Bengal election",
     gnews("TMC BJP West Bengal election clash violence arrest 2026")),
    ("statewide", "ANI: WB election Phase 1",
     gnews("West Bengal election Phase 1 2026 site:aninews.in")),
    ("statewide", "Telegraph India: WB election",
     gnews("West Bengal election 2026 site:telegraphindia.com")),
    ("statewide", "NDTV: WB election Phase 1",
     gnews("West Bengal assembly election Phase 1 2026 site:ndtv.com")),
    ("statewide", "Google: WB booth capture rigging 2026",
     gnews("West Bengal booth capture rigging poll violence 2026")),
    ("statewide", "Google: WB election candidate campaign 2026",
     gnews("West Bengal election candidate campaign Phase 1 Bankura Purulia 2026")),
    ("statewide", "Google: WB election CRPF deployment security",
     gnews("West Bengal election CRPF deployment security Phase 1 2026")),
    ("statewide", "Google: WB election ECI order Phase 1",
     gnews("West Bengal election 2026 ECI order announcement Phase 1 Bankura")),

    # ── ALERTS (24h only) ──────────────────────────────────────────────────
    ("alerts", "Google: WB election violence booth capture",
     gnews("West Bengal election violence booth capture 2026")),
    ("alerts", "Google: West Bengal MCC violation",
     gnews("West Bengal election MCC violation 2026")),
    ("alerts", "Google: EVM VVPAT complaint Bengal",
     gnews("EVM VVPAT complaint West Bengal 2026")),
    ("alerts", "Google: WB election arrest seized",
     gnews("West Bengal election arrest seized cash arms 2026")),
    ("alerts", "Google: Bengal bomb crude bomb election",
     gnews("Bengal bomb crude election 2026")),

    # ── OFFICIAL (24h only) ────────────────────────────────────────────────
    ("official", "Google: ECI West Bengal order",
     gnews("Election Commission West Bengal 2026 order directive")),
    ("official", "Google: CRPF paramilitary Bengal",
     gnews("CRPF paramilitary West Bengal election deployment 2026")),
    ("official", "Google: CEO West Bengal",
     gnews("CEO West Bengal election 2026")),
    ("official", "ANI: ECI paramilitary",
     gnews("Election Commission paramilitary 2026 ANI site:aninews.in")),

    # ── PARTY FEEDS (36h) — statements, press releases, allegations ────────
    # BJP
    ("parties", "BJP India: West Bengal election",
     gnews("BJP India West Bengal election 2026")),
    ("parties", "BJP West Bengal: statements",
     gnews("BJP4Bengal West Bengal election 2026 statement press release")),
    # INC / Congress
    ("parties", "INC India: West Bengal election",
     gnews("Congress INC West Bengal election 2026")),
    ("parties", "INC West Bengal: statements",
     gnews("Congress West Bengal Pradesh 2026 election statement")),
    # CPI(M)
    ("parties", "CPI(M) West Bengal: statements",
     gnews("CPIM CPI M West Bengal election 2026 statement")),
    # TMC / AITC
    ("parties", "TMC Trinamool: West Bengal election",
     gnews("TMC Trinamool AITC West Bengal election 2026 statement")),

    # ── BANGLA SOURCES — Bengali-language media (LLM translates to English) ─
    # Anandabazar Patrika — largest Bengali daily
    ("bangla", "Anandabazar Patrika: ভোট ২০২৬",
     gnews_bn("আনন্দবাজার বাঁকুড়া ভোট নির্বাচন ২০২৬")),
    ("bangla", "Anandabazar: WB election",
     gnews("site:anandabazar.com election vote 2026 West Bengal")),
    # Eisamay (Times of India Bengali)
    ("bangla", "Eisamay: WB election",
     gnews("site:eisamay.com election vote 2026 West Bengal")),
    # Zee 24 Ghanta — Bengali TV news
    ("bangla", "Zee 24 Ghanta: election",
     gnews("site:zee24ghanta.com election vote 2026")),
    # Bartaman Patrika
    ("bangla", "Bartaman: election 2026",
     gnews("site:bartamanpatrika.com election 2026")),
    # Bengali Google News — Bankura specific
    ("bangla", "Bengali News: বাঁকুড়া নির্বাচন",
     gnews_bn("বাঁকুড়া নির্বাচন ২০২৬ সহিংসতা বিধানসভা")),
    # Bengali Google News — WB Phase 1 general
    ("bangla", "Bengali News: পশ্চিমবঙ্গ ভোট",
     gnews_bn("পশ্চিমবঙ্গ বিধানসভা নির্বাচন ২০২৬ প্রথম দফা")),
    # ── YOUTUBE CHANNELS — via Google News index (direct YT RSS blocked server-side) ─
    # ABP Ananda (channel: UCwzOMowuG2q5Xgf9LIRJbSg)
    ("bangla", "ABP Ananda YouTube: election 2026",
     gnews("ABP Ananda election West Bengal 2026 site:youtube.com")),
    # Zee 24 Ghanta (channel: UCIvaYmXn910QMdemBG3v1pQ)
    ("bangla", "Zee 24 Ghanta YouTube: election 2026",
     gnews("Zee 24 Ghanta election West Bengal 2026 site:youtube.com")),
    # TV9 Bangla
    ("bangla", "TV9 Bangla YouTube: election 2026",
     gnews("TV9 Bangla election West Bengal 2026 site:youtube.com")),
]

# Social media monitoring URLs (shown in dashboard as clickable links, not RSS)
SOCIAL_MONITOR_LINKS = [
    ("X/Twitter", "Bankura election live", "https://twitter.com/search?q=Bankura+election&f=live"),
    ("X/Twitter", "#WestBengalElection2026", "https://twitter.com/search?q=%23WestBengalElection2026&f=live"),
    ("X/Twitter", "WB election violence", "https://twitter.com/search?q=West+Bengal+election+violence&f=live"),
    ("X/Twitter", "@ANI", "https://twitter.com/ANI"),
    ("X/Twitter", "@ECISVEEP", "https://twitter.com/ECISVEEP"),
    ("X/Twitter", "@BJP4Bengal", "https://twitter.com/BJP4Bengal"),
    ("X/Twitter", "@AITCofficial (TMC)", "https://twitter.com/AITCofficial"),
    ("X/Twitter", "@INCWestBengal", "https://twitter.com/INCWestBengal"),
    ("X/Twitter", "@CPIMWB", "https://twitter.com/CPIMWB"),
    ("Reddit", "r/WestBengal", "https://www.reddit.com/r/WestBengal/new/"),
    ("Reddit", "r/india WB election", "https://www.reddit.com/r/india/search/?q=west+bengal+election&sort=new"),
    ("Google News", "Bankura live", "https://news.google.com/search?q=Bankura+election+2026&hl=en-IN"),
    ("Google News", "WB election", "https://news.google.com/search?q=West+Bengal+election+2026&hl=en-IN"),
    ("Google News", "BJP Bengal", "https://news.google.com/search?q=BJP+West+Bengal+2026&hl=en-IN"),
    ("Google News", "TMC election", "https://news.google.com/search?q=TMC+Trinamool+election+2026&hl=en-IN"),
    ("Telegram", "WB police", "https://t.me/s/westbengalpolice"),
]

import urllib.parse

def _parse_pub_datetime(pub_str):
    """Parse RSS pubDate string to UTC datetime. Returns None on failure."""
    if not pub_str:
        return None
    try:
        import email.utils
        return email.utils.parsedate_to_datetime(pub_str)
    except Exception:
        pass
    # Try ISO format
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(pub_str.replace("Z", "+00:00"))
    except Exception:
        return None

def _age_hours(pub_str):
    """Return age of article in hours. Returns 9999 if unparseable."""
    dt = _parse_pub_datetime(pub_str)
    if dt is None:
        return 9999
    now = datetime.now(timezone.utc)
    diff = now - dt
    return diff.total_seconds() / 3600

def _relative_time(pub_str):
    """Convert RSS pubDate to human-readable relative time."""
    hours = _age_hours(pub_str)
    if hours >= 9999:
        return pub_str[:16] if pub_str else "recent"
    mins = int(hours * 60)
    if mins < 60:
        return f"{mins}m ago"
    elif hours < 24:
        return f"{int(hours)}h ago"
    else:
        return f"{int(hours/24)}d ago"

def fetch_rss(url, panel_key="statewide", timeout=10):
    """Fetch RSS feed with age-based filtering per panel."""
    max_age = MAX_AGE_HOURS.get(panel_key, 36)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()

        # Handle encoding
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            # Try stripping BOM or bad bytes
            data = data.lstrip(b'\xef\xbb\xbf')
            root = ET.fromstring(data)

        items = []
        seen_titles = set()
        # Handle both RSS <item> and Atom <entry>
        entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")

        for item in entries[:12]:  # check up to 12, filter by age
            title = (item.findtext("title") or
                     item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            # Clean source suffix from Google News titles
            title = re.sub(r"\s*[-|]\s*[A-Z][^|]{2,35}$", "", title).strip()
            if not title or len(title) < 8:
                continue
            fp = re.sub(r"\W+", "", title.lower())[:35]
            if fp in seen_titles:
                continue
            seen_titles.add(fp)

            # Get pubDate — multiple possible tag names
            pub = (item.findtext("pubDate") or
                   item.findtext("published") or
                   item.findtext("{http://www.w3.org/2005/Atom}published") or
                   item.findtext("updated") or
                   item.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()

            # AGE FILTER — drop items older than panel threshold
            age_h = _age_hours(pub)
            if age_h > max_age:
                continue  # skip old articles

            desc = (item.findtext("description") or
                    item.findtext("{http://www.w3.org/2005/Atom}summary") or
                    item.findtext("{http://www.w3.org/2005/Atom}content") or "").strip()
            desc = re.sub(r"<[^>]+>", "", desc).strip()

            source_el = item.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else ""
            if not source:
                # Try to extract from title or feed channel
                ch_title = root.findtext("channel/title") or root.findtext(".//title") or ""
                source = ch_title[:30] if ch_title else ""

            link = (item.findtext("link") or
                    item.findtext("{http://www.w3.org/2005/Atom}link") or "").strip()
            # Atom <link> is an attribute, not text
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.get("href", "")

            items.append({
                "title":    title,
                "desc":     desc[:400],
                "source":   source[:40],
                "time":     _relative_time(pub),
                "age_h":    age_h,
                "pub_raw":  pub,
                "url":      link,
            })

            if len(items) >= 8:
                break

        return sorted(items, key=lambda x: x["age_h"])  # newest first

    except Exception as e:
        return []

def fetch_all_news():
    """Fetch all RSS feeds in parallel with age filtering. Returns (context, panel_items, total)."""
    raw_results = {}
    threads = []

    def _fetch(key, label, url):
        items = fetch_rss(url, panel_key=key)
        if items:
            raw_results.setdefault(key, {})[label] = items

    for panel_key, label, url in RSS_FEEDS:
        th = threading.Thread(target=_fetch, args=(panel_key, label, url))
        th.daemon = True
        threads.append(th)
        th.start()

    for th in threads:
        th.join(timeout=18)

    ts    = datetime.now().strftime("%d %b %Y %H:%M IST")
    lines = [f"LIVE NEWS INTELLIGENCE — {ts}"]
    lines.append("=" * 60)

    panel_labels = {
        "bankura":   "BANKURA DISTRICT & LOCAL ACs (up to 3 days)",
        "surrounds": "SURROUNDING DISTRICTS — Purulia/Jhargram/Birbhum/Bardhaman/Medinipur (36h)",
        "statewide": "WEST BENGAL STATEWIDE (36h)",
        "parties":   "PARTY STATEMENTS — BJP / INC / CPI(M) / TMC (36h)",
        "alerts":    "ALERTS — Violence/MCC/EVM/Arrests (48h)",
        "official":  "OFFICIAL — ECI/CEO/Paramilitary + ANI (48h)",
        "bangla":    "BENGALI SOURCES — Anandabazar/Eisamay/Zee24Ghanta (translate to English)",
    }

    # Global dedup across all panels
    seen_fps = set()
    panel_items = {k: [] for k in panel_labels}

    for panel_key in ["alerts", "official", "bankura", "surrounds", "statewide", "parties", "bangla"]:
        label_map = raw_results.get(panel_key, {})
        candidates = []
        for label, items in label_map.items():
            for it in items:
                fp = re.sub(r"\W+", "", it["title"].lower())[:35]
                if fp not in seen_fps:
                    seen_fps.add(fp)
                    it["_label"] = label
                    candidates.append(it)
        # Sort by age, take freshest first
        candidates.sort(key=lambda x: x.get("age_h", 999))
        panel_items[panel_key] = candidates

    total = sum(len(v) for v in panel_items.values())

    # Build context string for LLM
    for panel_key, panel_label in panel_labels.items():
        items = panel_items[panel_key]
        lines.append(f"\n[{panel_label}]")
        max_h = MAX_AGE_HOURS.get(panel_key, 36)
        if not items:
            lines.append(f"  (no articles found within {max_h}h window)")
        else:
            for it in items[:7]:
                src  = f" [{it['source']}]" if it["source"] else ""
                url_tag = f" URL:{it['url']}" if it.get("url") else ""
                lines.append(f"  • [{it['time']}]{src}{url_tag}")
                lines.append(f"    {it['title']}")
                if it["desc"] and it["desc"][:80] not in it["title"]:
                    lines.append(f"    {it['desc'][:300]}")

    lines.append(f"\n{'='*60}")
    lines.append(f"Total unique articles (age-filtered): {total}")
    lines.append(f"Time windows: Bankura 72h | Surrounds/Statewide 36h | Alerts/Official 24h")
    lines.append("=" * 60)

    return "\n".join(lines), panel_items, total

# ── Backend adapters ──────────────────────────────────────────────────────────

def call_ollama(payload_dict, base_url, model):
    """Convert Anthropic-format payload → Ollama OpenAI-compat endpoint.
    Uses /v1/chat/completions (more reliable than /api/chat).
    Falls back to /api/chat if compat endpoint returns 404.
    """
    system   = payload_dict.get("system", "")
    messages = payload_dict.get("messages", [])

    ollama_messages = []
    if system:
        ollama_messages.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        ollama_messages.append({"role": m["role"], "content": content})

    # Try OpenAI-compat endpoint first (works with Ollama >= 0.1.24)
    for endpoint, parse_fn in [
        (
            base_url.rstrip("/") + "/v1/chat/completions",
            lambda d: d["choices"][0]["message"]["content"]
        ),
        (
            base_url.rstrip("/") + "/api/chat",
            lambda d: d.get("message", {}).get("content", "")
        ),
    ]:
        try:
            num_predict = payload_dict.get("max_tokens", 1000)
            body = json.dumps({
                "model":       model,
                "messages":    ollama_messages,
                "stream":      False,
                "temperature": 0.1,
                "options":     {"num_predict": num_predict, "num_ctx": 4096,
                               "temperature": 0.1},
                "think":       False,   # disable chain-of-thought for qwen3.x (saves 60% tokens)
            }).encode()
            req = urllib.request.Request(
                endpoint, data=body, method="POST",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=600) as r:
                data = json.loads(r.read())
            text = parse_fn(data)
            if text:
                print(f"  Ollama OK via {endpoint.split('/')[-2]}/{endpoint.split('/')[-1]}")
                return anthropic_response(text)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"  Ollama {e.code} on {endpoint} — {err_body[:120]}")
            if e.code == 404:
                continue   # try next endpoint
            raise
        except Exception as ex:
            print(f"  Ollama error on {endpoint}: {ex}")
            raise

    raise RuntimeError(
        f"Ollama returned 404 on both endpoints for model '{model}'.\n"
        f"Check: is Ollama running? Is model name correct?\n"
        f"Run:  ollama list   to see installed models.\n"
        f"Run:  ollama serve  to start Ollama if not running."
    )

def call_openai_compat(payload_dict, base_url, api_key, model):
    """Convert Anthropic-format payload → OpenAI chat completions (works for Groq too)."""
    system = payload_dict.get("system", "")
    messages = payload_dict.get("messages", [])

    oai_messages = []
    if system:
        oai_messages.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        oai_messages.append({"role": m["role"], "content": content})

    body = json.dumps({
        "model":       model,
        "messages":    oai_messages,
        "temperature": 0.1,
        "max_tokens":  4096,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body, method="POST", headers=headers
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())

    text = data["choices"][0]["message"]["content"]
    return anthropic_response(text)

def call_gemini(payload_dict, api_key, model):
    """Convert Anthropic-format payload → Gemini generateContent."""
    system   = payload_dict.get("system", "")
    messages = payload_dict.get("messages", [])

    parts = []
    if system:
        parts.append({"text": "SYSTEM INSTRUCTIONS:\n" + system + "\n\n"})
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        parts.append({"text": content})

    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
    }).encode()

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return anthropic_response(text)

def anthropic_response(text):
    """Wrap plain text in an Anthropic-format response envelope."""
    return {
        "id":           "local-" + str(int(time.time())),
        "type":         "message",
        "role":         "assistant",
        "model":        "local",
        "stop_reason":  "end_turn",
        "content":      [{"type": "text", "text": text}],
        "usage":        {"input_tokens": 0, "output_tokens": 0},
    }

def _repair_json_py(text):
    """
    Best-effort JSON extraction from raw LLM output.
    Handles: markdown fences, preamble text before {, truncated JSON,
    and the case where the model outputs prose instead of JSON.
    """
    text = text.strip()
    # Strip markdown fences
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    # Try direct parse first (happy path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the FIRST '{' and treat everything from there as JSON
    # This handles preamble text like "As an analyst, here is the JSON: {...}"
    brace_pos = text.find('{')
    if brace_pos > 0:
        text = text[brace_pos:]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    # Use greedy regex to find outermost {...} block
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    # Last resort: nothing parseable — return empty so fallback analysis is used
    return {}

def merge_prebuilt_with_analysis(llm_result, pre_built):
    """
    Combine Python-built feed arrays with the LLM's analysis block.
    The LLM writes: (1) analysis object, (2) bangla_translations[] — English strings by index.
    URLs, source, severity, time for bangla come from Python (pre_built["bangla"]).
    Translations are merged in: pre_built["bangla"][i]["summary"] = bangla_translations[i].
    """
    try:
        text = llm_result["content"][0]["text"]
        parsed = _repair_json_py(text)
        analysis            = parsed.get("analysis", {})
        bangla_translations = parsed.get("bangla_translations", [])
    except Exception as e:
        print(f"  merge: could not parse LLM analysis — {e}")
        analysis            = {}
        bangla_translations = []

    blank_analysis = {
        "headline": "Analysis unavailable — see raw feeds",
        "bankuraSituation": "", "surroundingSituation": "",
        "partyPositions": "",
        "keyRisks": ["", "", ""], "observerActions": ["", "", ""],
        "disinfoAlerts": "", "overallAssessment": "",
    }
    full = dict(pre_built)   # metrics + 7 arrays + topicCounts + figureMentions (from Python)
    full["analysis"] = analysis or blank_analysis

    # Merge LLM translations into Python-built bangla items (preserves RSS URLs)
    def _english_only(s):
        """Extract pure English from a string that may contain Bengali script.
        If the string has Bengali characters, find the last ':' or '—' separator
        and return the English part that follows it; otherwise return as-is."""
        import re
        if not s:
            return s
        has_bengali = bool(re.search(r'[\u0980-\u09FF]', s))
        if not has_bengali:
            return s.strip()
        # Try to find English segment after last ': ' or ' — '
        for sep in [': ', ' — ', ' - ', '। ']:
            parts = s.split(sep)
            # Find the last part that contains no Bengali
            for part in reversed(parts):
                if part.strip() and not re.search(r'[\u0980-\u09FF]', part):
                    return part.strip()
        # Fallback: strip all Bengali characters and extra whitespace
        cleaned = re.sub(r'[\u0980-\u09FF]+', ' ', s)
        cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' :—-')
        return cleaned if cleaned else s.strip()

    bangla_items = [dict(it) for it in full.get("bangla", [])]
    for i, translation in enumerate(bangla_translations):
        if i < len(bangla_items) and translation and translation.strip():
            bangla_items[i]["summary"] = _english_only(translation)
    full["bangla"] = bangla_items

    print(f"  Merge OK — analysis fields: {list(analysis.keys()) if analysis else 'NONE (fallback)'} | bangla items: {len(bangla_items)} ({len(bangla_translations)} translated)")
    return anthropic_response(json.dumps(full))

def _score_severity(text):
    """
    3-tier severity scorer.

    HIGH   — physical violence that actually happened: deaths, bombs, booth capture,
              abductions, firing, injuries during a clash or assault,
              arrests of candidates/agents/workers at a polling location.
    MEDIUM — MCC violations, EVM issues, political confrontation, 'clash' used
              metaphorically (no injury words present), arms/cash/liquor seizures,
              protest/tension, candidate list news, political analysis.
    LOW    — routine political statements, rally announcements, opinion pieces,
              candidate profiles, party strategy analysis.

    Rules:
      • 'clash' alone  → MEDIUM   (covers "Mamata's clash with ECI")
      • 'clash' + physical injury words → HIGH   (covers "clash leaves 3 injured")
      • 'arrested/detained' → HIGH only when at a polling booth / election-day context
      • 'analysis/why/opinion/strategy' patterns → cap at MEDIUM even if other words present
    """
    t = text.lower()

    # ── Cap analysis/opinion at MEDIUM regardless of other keywords ──────────
    _ANALYSIS_PATTERNS = [
        "why ", " sees advantage", " strategy", "political analysis",
        "opinion:", "analysis:", "explainer", " explained", "here's why",
        "what it means", "sees benefit", "mamata's strategy", "bjp strategy",
        "tmc strategy",
    ]
    is_analysis = any(p in t for p in _ANALYSIS_PATTERNS)

    # ── Definite HIGH: physical violence, no context needed ──────────────────
    # NOTE: bare "bullet" removed — "bulletproof" contains it and is not violence.
    # "shot dead/shot at/firing" already cover all genuine gunfire scenarios.
    _DEFINITE_HIGH = [
        "killed", "bomb", "crude bomb",
        "firing", "shot dead", "shot at",
        "booth capture", "booth looting",
        "abduct", "kidnap",
        "riot", "arson",
        "stone pelting",
    ]
    if not is_analysis and any(k in t for k in _DEFINITE_HIGH):
        return "high"

    # ── "murder" only HIGH for current election violence ─────────────────────
    # Historical crime references (RG Kar case, past murder cases) should not
    # inflate the threat level — they are MEDIUM at most.
    _MURDER_HISTORICAL = [
        "murder victim", "rape-murder", "murder case", "murder accused",
        "murder probe", "murder convict", "murder suspect", "murder trial",
        "murder of", "who was murdered",
    ]
    if not is_analysis and "murder" in t:
        if not any(p in t for p in _MURDER_HISTORICAL):
            return "high"
        # else: historical reference — fall through to MEDIUM

    # ── Conditional HIGH: 'clash/attack/assault/loot' only if injury words present ──
    _PHYSICAL_INJURY = ["injur", "hurt", "wound", "hospitalised", "dead", "died", "bleed"]
    _CONDITIONAL_HIGH_TRIGGER = ["clash", "attack", "assault", "loot"]
    has_injury = any(k in t for k in _PHYSICAL_INJURY)
    if not is_analysis and has_injury and any(k in t for k in _CONDITIONAL_HIGH_TRIGGER):
        return "high"

    # ── Conditional HIGH: arrests only if election-day / booth-specific context ──
    _ARREST_CONTEXT = [
        "during polling", "on polling day", "election day",
        "at booth", "polling booth", "at polling",
        "candidate arrested", "agent arrested", "worker arrested",
        "poll worker", "presiding officer",
    ]
    if not is_analysis and any(k in t for k in ["arrested", "detained"]):
        if any(ctx in t for ctx in _ARREST_CONTEXT):
            return "high"

    # ── MEDIUM: everything below clear violence but above routine news ───────
    _MEDIUM_KW = [
        "mcc violation", "model code violation",
        "seized", "cash seized", "liquor seized", "arms seized", "illegal cash",
        "tension", "clash", "confrontation", "blocked", "stopped",
        "evm", "vvpat", "malfunction", "tamper",
        "threat", "intimidat",
        "protest", "agitation", "arrested", "detained",   # general arrest = medium
        "complaint", "allegation", "violation",
        "second list", "candidate list", "announces list",  # candidate news = medium
    ]
    if any(k in t for k in _MEDIUM_KW):
        return "medium"

    return "low"

def _topic_counts(panel_items):
    """Count hits per sidebar topic chip."""
    all_text = lambda items: " ".join(
        (it["title"]+" "+it["desc"]).lower() for it in items
    )
    def hits(items, kws):
        t = all_text(items)
        return sum(1 for k in kws if k in t)
    bi = panel_items.get("bankura", [])
    si = panel_items.get("surrounds", [])
    ai = panel_items.get("alerts", [])
    oi = panel_items.get("official", [])
    wi = panel_items.get("statewide", [])
    pi = panel_items.get("parties", [])
    bni = panel_items.get("bangla", [])
    every = bi+si+ai+oi+wi+pi+bni
    return [
        hits(bi+ai, ["bankura","violence","clash","attack","bomb"]),
        hits(bi,    ["bankura","bishnupur"]),
        hits(si,    ["purulia","jhargram"]),
        hits(si,    ["birbhum","bardhaman"]),
        hits(every, ["mcc","model code","violation"]),
        hits(every+pi, ["tmc","bjp","clash","confrontation","congress","cpim","trinamool"]),
        hits(every, ["evm","vvpat"]),
        hits(oi,    ["paramilitary","crpf","bsf","cisf","deployment"]),
    ]

def _threat_level(violence_count, mcc_count, high_items):
    """
    Compute threat level from WB-filtered article signals.

    Thresholds raised vs previous version because:
    - violence_keywords now covers ONLY physical incidents (not 'clash/arrested')
    - high_items comes from a tighter _score_severity() that no longer marks
      analysis pieces or non-WB arrests as HIGH
    - Input is WB-geo-filtered so national noise is excluded

    Scale:
      CRITICAL  — multiple confirmed violent incidents; serious pre-poll breakdown
      HIGH      — confirmed violence or several high-severity MCC/EVM incidents
      MODERATE  — single violence report OR several MCC/liquor/cash seizure reports
      LOW       — routine pre-election activity, no confirmed violence
    """
    if violence_count >= 4 or high_items >= 7:
        return "CRITICAL"
    if violence_count >= 2 or high_items >= 4:
        return "HIGH"
    if violence_count >= 1 or mcc_count >= 3 or high_items >= 2:
        return "MODERATE"
    return "LOW"

def _build_panel_json(items, max_items=5):
    """Pre-build JSON array for a panel from Python-scored items."""
    out = []
    for it in items[:max_items]:
        severity = _score_severity(it["title"] + " " + it["desc"])
        source   = it.get("source") or "Google News"
        # Truncate title to use as placeholder summary — LLM will rewrite
        summary_hint = it["title"][:120]
        out.append({
            "source":       source,
            "summary_hint": summary_hint,
            "desc":         it.get("desc","")[:300],
            "severity":     severity,
            "time":         it.get("time","recent"),
            "url":          it.get("url",""),
        })
    return out

def inject_news_into_payload(payload_dict):
    """
    Two-stage pipeline:
      Stage 1 (Python): fetch RSS, deduplicate, score severity, build all 6 feed arrays directly
      Stage 2 (LLM):    receives richer brief (5 items + descriptions), writes ANALYSIS ONLY
    Merging happens in do_POST() via merge_prebuilt_with_analysis().
    """
    print("  Fetching live RSS news feeds...")
    news_context, panel_items, count = fetch_all_news()
    print(f"  Fetched {count} unique headlines | Pre-classifying...")

    ts = datetime.now().strftime("%d %b %Y %H:%M IST")

    # ── Stage 1: Pure Python scoring, classification & array building ────────

    # ── Filter 1: Election relevance ─────────────────────────────────────────
    # Items must mention at least one election keyword to pass.
    # NOTE: bare "seize/arrest/clash" removed — too broad, catches non-WB national news.
    # Those words remain valid for severity scoring AFTER geographic check passes.
    _ELECTION_KW = [
        "election", "vote", "voting", "voter", "electi", "candidate", "constituency",
        "assembly", "mcc", "model code", "booth", "evm", "vvpat", "ballot",
        "polling", "poll station", "phase 1", "phase1",
        "tmc", "trinamool", "bjp", "congress", "cpim", "cpi(m)", "left front",
        "mamata", "campaign", "rally", "nomination", "manifesto",
        "paramilitary", "crpf", "bsf",
        "eci", "election commission", "ceo west bengal", "returning officer",
        "bankura", "purulia", "jhargram", "birbhum", "bardhaman", "medinipur",
        "bishnupur", "saltora", "chhatna", "ranibandh", "taldangra", "barjora", "onda",
        "kotulpur", "sonamukhi", "indas", "2026",
    ]
    def _is_election_relevant(item):
        text = (item.get("title", "") + " " + item.get("desc", "")).lower()
        return any(kw in text for kw in _ELECTION_KW)

    # ── Filter 2: West Bengal geographic relevance ────────────────────────────
    # Statewide panel (and violence counting) must be anchored to WB/Bengal.
    # This drops Delhi gun seizures, UP/Bihar/MP election news, etc.
    # News from OTHER poll-bound states is dropped unless it has direct WB bearing.
    _WB_GEO_KW = [
        "west bengal", "bengal", "wb ",
        "bankura", "purulia", "jhargram", "birbhum", "bardhaman", "burdwan",
        "medinipur", "howrah", "hooghly", "kolkata", "calcutta",
        "murshidabad", "malda", "nadia", "cooch behar", "darjeeling", "jalpaiguri",
        "north 24", "south 24", "24 pargana",
        "bishnupur", "saltora", "chhatna", "ranibandh", "taldangra", "barjora",
        "onda", "kotulpur", "sonamukhi", "indas", "raipur",
        "mamata", "trinamool", "tmc", "wb election", "wb 2026",
        "ceo west bengal", "bengal election",
    ]
    def _is_wb_relevant(item):
        text = (item.get("title", "") + " " + item.get("desc", "")).lower()
        return any(kw in text for kw in _WB_GEO_KW)

    # Statewide: election-relevant AND WB-geographic
    statewide_raw = [
        it for it in panel_items.get("statewide", [])
        if _is_election_relevant(it) and _is_wb_relevant(it)
    ]
    # Alerts: Google News RSS for violence/arms/MCC often pulls national crime news.
    # Apply WB geo-filter so Delhi/UP/Bihar seizures never appear in alerts panel.
    alerts_raw = [
        it for it in panel_items.get("alerts", [])
        if _is_wb_relevant(it)
    ]
    # Parties: BJP/TMC press-release feeds also pull national party news.
    # Filter to WB-relevant items (mentions Bengal/Mamata/TMC/district names).
    parties_raw = [
        it for it in panel_items.get("parties", [])
        if _is_wb_relevant(it)
    ]
    # Bankura, surrounds, official, bangla: sourced from WB-specific feeds — no filter needed.

    # Log how much was dropped across all filtered panels
    sw_dropped = len(panel_items.get("statewide", [])) - len(statewide_raw)
    al_dropped = len(panel_items.get("alerts",    [])) - len(alerts_raw)
    pa_dropped = len(panel_items.get("parties",   [])) - len(parties_raw)
    total_dropped = sw_dropped + al_dropped + pa_dropped
    if total_dropped:
        print(f"  Geo-filter: dropped {total_dropped} non-WB article(s) "
              f"[statewide:{sw_dropped} alerts:{al_dropped} parties:{pa_dropped}]")

    bankura_panel   = _build_panel_json(panel_items.get("bankura",   []))
    surrounds_panel = _build_panel_json(panel_items.get("surrounds", []))
    statewide_panel = _build_panel_json(statewide_raw)
    parties_panel   = _build_panel_json(parties_raw)
    alerts_panel    = _build_panel_json(alerts_raw)
    official_panel  = _build_panel_json(panel_items.get("official",  []))
    bangla_panel    = _build_panel_json(panel_items.get("bangla",    []))

    all_panels = (bankura_panel + surrounds_panel + statewide_panel +
                  parties_panel + alerts_panel + official_panel + bangla_panel)

    # ── Violence / MCC counting: WB-only items, tighter keyword list ─────────
    # 'clash', 'arrested', 'detained' removed from violence keywords —
    # they are too broad and inflate counts when national news leaks through.
    # Only actual physical violence events count toward threat level.
    violence_keywords = [
        "killed", "murder", "bomb", "crude bomb", "firing", "shot dead", "shot at",
        "booth capture", "booth looting",
        "abduct", "kidnap",
        "assault", "riot", "arson", "loot",
        "stone pelting", "injured", "hurt", "violence",
    ]
    mcc_keywords = [
        "mcc violation", "model code violation",
        "liquor seized", "cash seized", "arms seized", "illegal cash",
        "bribe", "inducement",
    ]

    def kw_count(items, kws):
        n = 0
        for it in items:
            text = (it.get("title","") + " " + it.get("desc","")).lower()
            if any(k in text for k in kws):
                n += 1
        return n

    # Violence and MCC counts use ONLY geo-filtered items.
    # alerts_raw and parties_raw are now WB-filtered (Delhi/national items removed).
    wb_raw = (panel_items.get("bankura",[]) + panel_items.get("surrounds",[]) +
              statewide_raw + alerts_raw + parties_raw +
              panel_items.get("official",[]) + panel_items.get("bangla",[]))

    violence_count = kw_count(wb_raw, violence_keywords)
    mcc_count      = kw_count(wb_raw, mcc_keywords)
    high_items     = sum(1 for it in all_panels if it["severity"] == "high")
    threat_level   = _threat_level(violence_count, mcc_count, high_items)
    topic_counts   = _topic_counts(panel_items)

    print(f"  Signals: {count} | Violence hits: {violence_count} | MCC hits: {mcc_count} | Threat: {threat_level}")

    # Build Python feed arrays — LLM never touches these
    BANGLA_MAX = 6   # items sent to LLM for translation — must match panel_brief max_items

    def build_feed_array(panel):
        return [{
            "source":   it["source"],
            "summary":  it["summary_hint"][:150],   # full headline as summary
            "severity": it["severity"],
            "time":     it["time"],
            "url":      it.get("url", ""),
        } for it in panel]

    pre_built = {
        "metrics": {
            "totalSignals": count, "highAlerts": high_items,
            "violenceReports": violence_count, "threatLevel": threat_level,
        },
        "bankura":   build_feed_array(bankura_panel),
        "surrounds": build_feed_array(surrounds_panel),
        "statewide": build_feed_array(statewide_panel),
        "parties":   build_feed_array(parties_panel),
        "alerts":    build_feed_array(alerts_panel),
        "official":  build_feed_array(official_panel),
        "bangla":    build_feed_array(bangla_panel[:BANGLA_MAX]),  # capped = exact match with LLM prompt
        "topicCounts": topic_counts,
    }

    # ── Scan all items for political figure mentions ─────────────────────────
    # Use all WB-relevant items + parties panel (party statements mention candidates)
    figure_scan_items = wb_raw + panel_items.get("parties", [])
    figure_mentions = []
    for fig in POLITICAL_FIGURES:
        matches = []
        for it in figure_scan_items:
            text = (it.get("title","") + " " + it.get("desc","")).lower()
            if any(kw in text for kw in fig["keywords"]):
                matches.append({
                    "headline": it.get("title","")[:120],
                    "url":      it.get("url",""),
                    "time":     it.get("time",""),
                    "severity": _score_severity(it.get("title","") + " " + it.get("desc","")),
                })
        figure_mentions.append({
            "name":     fig["name"],
            "party":    fig["party"],
            "role":     fig["role"],
            "mentions": matches[:3],
            "count":    len(matches),
        })
    mentioned = sum(1 for f in figure_mentions if f["count"] > 0)
    print(f"  Figure monitor: {mentioned}/{len(POLITICAL_FIGURES)} figures mentioned in feeds")
    pre_built["figureMentions"] = figure_mentions

    # ── Phase 4: per-AC threat metrics for map overlay ────────────────────────
    pre_built["acMetrics"] = _build_ac_metrics(panel_items, figure_mentions)

    # ── Phase 4B: escalation briefing check ───────────────────────────────────
    _check_escalation(threat_level)

    # ── Phase 2: queue new MED/HIGH articles for background digesting ──────────
    _save_digest_candidates(panel_items)

    # Stash for do_POST() to merge after LLM returns
    payload_dict["_pre_built"] = pre_built

    # ── Stage 2: Richer brief → LLM writes ANALYSIS ONLY ────────────────────
    # Now 5 items per panel with description snippet — more context for the LLM
    def panel_brief(panel, name, max_items=5, numbered=False):
        if not panel:
            return f"\n[{name}]: no articles"
        lines = [f"\n[{name}]"]
        for idx, it in enumerate(panel[:max_items]):
            sev  = it["severity"].upper()[0]   # H / M / L
            desc = it.get("desc", "")
            desc_snippet = (": " + desc[:120].rstrip()) if desc else ""
            prefix = f"{idx}. " if numbered else "  "
            lines.append(f"{prefix}[{sev}] {it['source']}: {it['summary_hint'][:120]}{desc_snippet}")
        return "\n".join(lines)

    brief = (
        f"NEWS BRIEF — {ts}\n"
        f"WB Assembly Election Phase 1: 23 April 2026\n"
        f"Observer: Bankura district. Covered: Bankura, Purulia, Jhargram, Birbhum, "
        f"Paschim Bardhaman, Paschim Medinipur\n"
        f"Signals: {count} total | Violence: {violence_count} | MCC: {mcc_count} | "
        f"Threat level: {threat_level}\n"
        + panel_brief(bankura_panel,   "BANKURA DISTRICT & ACs")
        + panel_brief(surrounds_panel, "SURROUNDING DISTRICTS")
        + panel_brief(statewide_panel, "WB STATEWIDE")
        + panel_brief(parties_panel,   "PARTY STATEMENTS BJP/INC/CPIM/TMC")
        + panel_brief(alerts_panel,    "ALERTS VIOLENCE/MCC/EVM")
        + panel_brief(official_panel,  "OFFICIAL ECI/CEO/PARAMILITARY")
        + panel_brief(bangla_panel,    "BENGALI SOURCES — translate headlines to English",
                      max_items=BANGLA_MAX, numbered=True)
    )

    system_prompt = (
        f"You are an election intelligence analyst briefing an official observer in Bankura district, "
        f"West Bengal. Phase 1 election date: 23 April 2026. Current time: {ts}.\n"
        f"You understand Bengali. Translate any Bengali headlines to English accurately.\n"
        f"CRITICAL INSTRUCTION: Respond with ONLY a raw JSON object. "
        f"Do NOT write any introduction, preamble, disclaimer, acknowledgement, or explanation. "
        f"Do NOT say 'Here is', 'As an analyst', 'I will', 'Based on', or anything similar. "
        f"Your ENTIRE response must be valid JSON starting with {{ and ending with }}. "
        f"Required format:\n"
        f'{{"analysis":{{"headline":"","bankuraSituation":"","surroundingSituation":"",'
        f'"partyPositions":"","keyRisks":["","",""],"observerActions":["","",""],'
        f'"disinfoAlerts":"","overallAssessment":""}},'
        f'"bangla_translations":["Pure English translation only — no Bengali script","Pure English only"]}}'
    )

    user_prompt = (
        f"{brief}\n\n"
        f"Task 1 — ANALYSIS: Write a concise intelligence analysis for the observer.\n"
        f"- headline: one sentence capturing the most important development\n"
        f"- bankuraSituation: 2-3 sentences on Bankura district specifically\n"
        f"- surroundingSituation: 2-3 sentences on surrounding districts\n"
        f"- partyPositions: key party actions, statements, or incidents\n"
        f"- keyRisks: exactly 3 specific risks the observer should watch for\n"
        f"- observerActions: exactly 3 concrete recommended actions\n"
        f"- disinfoAlerts: any rumours, fake news, or coordinated narratives detected (or 'None detected')\n"
        f"- overallAssessment: 2-3 sentence overall situation assessment\n\n"
        f"Task 2 — BENGALI SOURCES: Items 0,1,2... listed under [BENGALI SOURCES] above. "
        f"For EACH item write ONE English-only string: the meaning of the Bengali headline + 1-sentence context. "
        f"CRITICAL: Write ONLY English letters/words. Do NOT copy or reproduce any Bengali/Devanagari script. "
        f"If a headline mixes English and Bengali, still output the full meaning in plain English only. "
        f"bangla_translations[0] = English meaning of item 0, [1] = item 1, etc. "
        f"If no Bengali items return [].\n\n"
        f"YOUR RESPONSE MUST START WITH {{ — output the JSON object now:"
    )

    total_chars = len(system_prompt) + len(user_prompt)
    approx_tokens = total_chars // 4
    print(f"  Prompt size: ~{approx_tokens} tokens ({total_chars} chars) → LLM writes analysis + bangla translations (~1000 tokens out)")

    payload_dict["system"]   = system_prompt
    payload_dict["messages"] = [{"role": "user", "content": user_prompt}]
    payload_dict.pop("tools",       None)
    payload_dict.pop("tool_choice", None)
    payload_dict["max_tokens"] = 1400   # analysis (~700) + bangla_translations 6×~40tok (~240) + headroom

    return payload_dict


# ── Phase 4B: Daily Briefing (Telegram Bot + Fast2SMS fallback) ───────────────
#
# RISK & MITIGATION (hardcoded per design):
#
#   RISK 1 — Telegram bot token exposed / unauthorised sends
#     MITIGATION: Token stored only in local SQLite (never committed — .gitignore).
#                 Displayed as password field in UI. Anyone with the token can send
#                 to the bot, but the bot only delivers to the registered chat_id.
#
#   RISK 2 — Telegram unreachable on field network (govt/hotel Wi-Fi blocks)
#     MITIGATION: Fast2SMS SMS fallback. Enabled only when api_key is configured.
#                 SMS is intentionally DISABLED until user supplies Fast2SMS key.
#
#   RISK 3 — Duplicate escalation alerts (same HIGH cycle fires twice)
#     MITIGATION: Dedup by (threat_level, cycle_id) — if a send record already
#                 exists for this cycle at this threat level, skip silently.
#
#   RISK 4 — SMS API key stored in plaintext in local DB
#     MITIGATION: DB file is local-only (never committed — in .gitignore).
#                 Proxy prints a warning on startup if key is set.
#
#   RISK 5 — Proxy not running at scheduled send time
#     MITIGATION: Scheduler logs missed sends. On restart, if last_sent is more
#                 than 23h ago AND current time is past schedule_time, sends once.
#
#   RISK 6 — Brief too long for SMS (160 char limit per segment)
#     MITIGATION: SMS variant is separately formatted to ≤320 chars (2 segments).
#                 Telegram variant uses HTML mode; split into 2 messages if >4096.

FEATURE_BRIEFING = True    # master switch — set False to disable entirely
BRIEFING_SMS_ENABLED = False  # SMS via Fast2SMS — OFF until user supplies API key

# ── Briefing config helpers ───────────────────────────────────────────────────

_BRIEFING_DEFAULTS = {
    "enabled":                "false",
    "telegram_bot_token":     "",      # from @BotFather — stored as password in UI
    "telegram_chat_id":       "",      # auto-detected or pasted from /api/telegram/chatid
    "schedule_time":          "08:00", # IST, 24h format
    "escalate_on_high":       "true",
    "escalate_on_critical":   "true",
    "election_day_interval":  "120",   # minutes between briefings on election day
    "sms_provider":           "fast2sms",
    "sms_api_key":            "",      # Fast2SMS key — blank = SMS disabled
    "sms_phone":              "",      # +91XXXXXXXXXX
}

def _load_briefing_config():
    """Return briefing config dict (DB values merged over defaults)."""
    cfg = dict(_BRIEFING_DEFAULTS)
    if not FEATURE_SQLITE:
        return cfg
    try:
        with _db_lock:
            c = _db_conn()
            rows = c.execute("SELECT key, value FROM briefing_config").fetchall()
            c.close()
        for row in rows:
            cfg[row["key"]] = row["value"]
    except Exception:
        pass
    return cfg

def _save_briefing_config(updates):
    """Persist a dict of key→value pairs into briefing_config table."""
    if not FEATURE_SQLITE:
        return
    with _db_lock:
        c = _db_conn()
        for k, v in updates.items():
            c.execute("INSERT OR REPLACE INTO briefing_config(key,value) VALUES(?,?)", (k, str(v)))
        c.commit(); c.close()

def _briefing_already_sent(trigger, cycle_id, threat_level):
    """Return True if an identical briefing was already sent for this cycle."""
    if not FEATURE_SQLITE:
        return False
    try:
        with _db_lock:
            c = _db_conn()
            n = c.execute("""
                SELECT COUNT(*) FROM briefing_sends
                WHERE trigger=? AND cycle_id=? AND threat_level=?
            """, (trigger, cycle_id, threat_level)).fetchone()[0]
            c.close()
        return n > 0
    except Exception:
        return False

def _log_briefing_send(trigger, threat_level, headline, ntfy_status, sms_status, cycle_id):
    if not FEATURE_SQLITE:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        c = _db_conn()
        c.execute("""
            INSERT INTO briefing_sends
              (sent_at, trigger, threat_level, headline, ntfy_status, sms_status, cycle_id)
            VALUES (?,?,?,?,?,?,?)
        """, (now, trigger, threat_level, headline[:300], ntfy_status, sms_status, cycle_id))
        c.commit(); c.close()

# ── Brief formatter ───────────────────────────────────────────────────────────

def _format_brief_telegram(parsed, trigger, ts):
    """
    Format a full briefing for Telegram (HTML parse_mode).
    Telegram HTML supports: <b>, <i>, <code>, <pre>.
    Returns list of message strings — usually 1, max 2 (split if >4096 chars).
    """
    m  = parsed.get("metrics", {})
    an = parsed.get("analysis", {})
    tl = m.get("threatLevel", "LOW")
    tl_emoji = {"LOW":"🟢","MODERATE":"🟡","HIGH":"🔴","CRITICAL":"🚨"}.get(tl, "🔵")

    trigger_label = {
        "scheduled":  "[Scheduled]",
        "escalation": "[ESCALATION ALERT]",
        "manual":     "[Manual test]",
    }.get(trigger, "[Brief]")

    risks   = an.get("keyRisks", [])
    actions = an.get("observerActions", [])

    lines = [
        f"<b>{trigger_label} — {ts}</b>",
        f"{'—'*28}",
        f"Threat: {tl_emoji} <b>{tl}</b>  |  Signals: {m.get('totalSignals',0)}  |  High: {m.get('highAlerts',0)}",
        f"",
        f"<b>HEADLINE</b>",
        an.get("headline", "No analysis available"),
        f"",
    ]
    if risks:
        lines.append("<b>KEY RISKS</b>")
        for r in risks:
            if r: lines.append(f"• {r}")
        lines.append("")
    if actions:
        lines.append("<b>OBSERVER ACTIONS</b>")
        for a in actions:
            if a: lines.append(f"• {a}")
        lines.append("")
    disinfo = an.get("disinfoAlerts", "")
    if disinfo and disinfo.lower() not in ("none detected", "none", ""):
        lines.append(f"⚠️ <b>DISINFO:</b> {disinfo}")
        lines.append("")
    assessment = an.get("overallAssessment", "")
    if assessment:
        lines.append(f"<i>{assessment}</i>")

    full = "\n".join(lines).strip()

    # Split into ≤4096-char messages (Telegram limit) at paragraph boundaries
    if len(full) <= 4096:
        return [full]
    split_at = full.rfind("\n\n", 0, 4000)
    if split_at == -1:
        split_at = 4000
    return [full[:split_at].strip(), full[split_at:].strip()]

def _format_brief_sms(parsed, trigger, ts):
    """Format a compact briefing for SMS (≤320 chars, 2 segments)."""
    m  = parsed.get("metrics", {})
    an = parsed.get("analysis", {})
    tl = m.get("threatLevel", "LOW")
    headline = an.get("headline","—")[:120]
    risk0 = (an.get("keyRisks") or [""])[0][:80]
    action0 = (an.get("observerActions") or [""])[0][:80]
    msg = (
        f"WB Intel {ts[:10]} | {tl}\n"
        f"{headline}\n"
        f"Risk: {risk0}\n"
        f"Action: {action0}"
    )
    return msg[:320]

# ── Send functions ────────────────────────────────────────────────────────────

def _send_telegram(token, chat_id, messages):
    """
    Send one or more messages to a Telegram chat via the Bot API.
    messages: list of HTML-formatted strings (from _format_brief_telegram).
    Returns ('ok', 'N sent') or ('error', reason) or ('skip', reason).
    """
    if not token or not token.strip():
        return ("skip", "bot token not configured")
    if not chat_id or not chat_id.strip():
        return ("skip", "chat_id not configured — use Auto-detect or paste from Telegram")
    sent = 0
    for msg in messages:
        try:
            payload = json.dumps({
                "chat_id":    chat_id.strip(),
                "text":       msg,
                "parse_mode": "HTML",
            }).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token.strip()}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                if not data.get("ok"):
                    return ("error", str(data.get("description", data))[:120])
                sent += 1
        except Exception as e:
            return ("error", str(e)[:120])
    return ("ok", f"{sent} message(s) sent")

def _send_sms_fast2sms(api_key, phone, message):
    """
    Send SMS via Fast2SMS DLT route.
    INTENTIONALLY INACTIVE — BRIEFING_SMS_ENABLED=False until user supplies key.
    Returns ('skip', reason) when disabled.
    """
    if not BRIEFING_SMS_ENABLED:
        return ("skip", "SMS disabled — set BRIEFING_SMS_ENABLED=True and supply Fast2SMS API key")
    if not api_key or not phone:
        return ("skip", "no api_key or phone configured")
    try:
        phone_clean = re.sub(r"[^\d]", "", phone)[-10:]   # last 10 digits
        body = json.dumps({
            "route":   "v3",
            "sender_id": "WBINTEL",
            "message": message,
            "language": "english",
            "flash":   0,
            "numbers": phone_clean,
        }).encode()
        req = urllib.request.Request(
            "https://www.fast2sms.com/dev/bulkV2",
            data=body,
            headers={
                "authorization": api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data.get("return"):
                return ("ok", data.get("message","sent"))
            return ("error", str(data)[:120])
    except Exception as e:
        return ("error", str(e)[:120])

# ── Orchestrator ──────────────────────────────────────────────────────────────

_last_escalation_threat = "LOW"   # track previous threat level for escalation detection

def _send_briefing(trigger="manual"):
    """
    Build and dispatch a briefing from the latest cycle in DB.
    trigger: 'scheduled' | 'escalation' | 'manual'
    """
    if not FEATURE_BRIEFING:
        return {"tg": "skip", "sms": "skip", "reason": "feature disabled"}

    cfg = _load_briefing_config()
    if cfg["enabled"] != "true" and trigger != "manual":
        return {"tg": "skip", "sms": "skip", "reason": "briefing disabled"}

    tg_token  = cfg.get("telegram_bot_token", "").strip()
    tg_chatid = cfg.get("telegram_chat_id", "").strip()
    sms_key   = cfg.get("sms_api_key", "").strip()
    sms_phone = cfg.get("sms_phone", "").strip()

    # Fetch latest cycle from DB
    cycle_id = None
    parsed   = {}
    ts       = datetime.now().strftime("%d %b %Y %H:%M IST")
    if FEATURE_SQLITE:
        try:
            with _db_lock:
                c = _db_conn()
                row = c.execute(
                    "SELECT id, fetched_at, full_json FROM cycles ORDER BY id DESC LIMIT 1"
                ).fetchone()
                c.close()
            if row:
                cycle_id = row["id"]
                raw_ts   = row["fetched_at"] or ""
                if raw_ts:
                    try:
                        dt = datetime.fromisoformat(raw_ts).astimezone()
                        ts = dt.strftime("%d %b %Y %H:%M IST")
                    except Exception:
                        ts = raw_ts
                parsed   = json.loads(row["full_json"] or "{}")
        except Exception as e:
            print(f"  [briefing] DB read error: {e}")

    threat_level = (parsed.get("metrics") or {}).get("threatLevel", "LOW")
    headline     = (parsed.get("analysis") or {}).get("headline", "No analysis available")

    # Dedup: skip if already sent for this cycle+trigger+threat combo
    if cycle_id and _briefing_already_sent(trigger, cycle_id, threat_level):
        print(f"  [briefing] Dedup skip — already sent ({trigger}, cycle {cycle_id})")
        return {"tg": "skip", "sms": "skip", "reason": "already sent"}

    # Format briefs
    tg_messages = _format_brief_telegram(parsed, trigger, ts)
    sms_body    = _format_brief_sms(parsed, trigger, ts)

    # Send Telegram
    tg_status, tg_detail = _send_telegram(tg_token, tg_chatid, tg_messages)
    print(f"  [briefing] telegram → {tg_status} ({tg_detail}) | chat={tg_chatid[:8]}…")

    # Send SMS (only if enabled + configured)
    sms_status, sms_detail = _send_sms_fast2sms(sms_key, sms_phone, sms_body)
    if sms_status != "skip":
        print(f"  [briefing] sms → {sms_status} ({sms_detail})")

    # Log to DB (ntfy_status column reused for Telegram status)
    _log_briefing_send(trigger, threat_level, headline, tg_status, sms_status, cycle_id)

    return {
        "tg":           tg_status,
        "tg_detail":    tg_detail,
        "sms":          sms_status,
        "sms_detail":   sms_detail,
        "threat_level": threat_level,
        "headline":     headline,
    }

def _check_escalation(new_threat):
    """
    Compare new threat level against last known.
    Fire an escalation briefing if threshold crossed upward.
    Runs in a fire-and-forget thread so it never blocks the fetch cycle.
    """
    global _last_escalation_threat
    cfg = _load_briefing_config()
    prev = _last_escalation_threat
    _last_escalation_threat = new_threat

    LEVELS = {"LOW":0,"MODERATE":1,"HIGH":2,"CRITICAL":3}
    prev_n = LEVELS.get(prev, 0)
    new_n  = LEVELS.get(new_threat, 0)

    if new_n <= prev_n:
        return   # no escalation

    should_alert = (
        (new_threat == "HIGH"     and cfg.get("escalate_on_high","true")     == "true") or
        (new_threat == "CRITICAL" and cfg.get("escalate_on_critical","true") == "true")
    )
    if not should_alert:
        return

    print(f"  [briefing] Threat escalated {prev}→{new_threat} — sending escalation brief")
    threading.Thread(
        target=_send_briefing,
        args=("escalation",),
        daemon=True,
        name="briefing-escalation",
    ).start()

# ── Scheduler loop ────────────────────────────────────────────────────────────

def _start_briefing_scheduler():
    """Launch the daily briefing scheduler as a daemon thread."""
    if not FEATURE_BRIEFING:
        return

    def _scheduler_loop():
        print("  [briefing] Scheduler started")
        last_scheduled_date = None

        while True:
            try:
                time.sleep(60)   # check every minute
                cfg = _load_briefing_config()
                if cfg.get("enabled") != "true":
                    continue

                now_ist = datetime.now()   # local time (proxy runs in IST)
                today   = now_ist.date()

                # ── Election day: every N minutes between 07:00–18:00 ──────
                is_election_day = (today.year == 2026 and today.month == 4 and today.day == 23)
                if is_election_day:
                    interval_min = int(cfg.get("election_day_interval", "120"))
                    # Check last send time
                    last_sent = _last_briefing_sent_at()
                    if last_sent:
                        minutes_since = (datetime.now(timezone.utc) -
                                         datetime.fromisoformat(last_sent)).total_seconds() / 60
                        if minutes_since < interval_min:
                            continue
                    h = now_ist.hour
                    if 7 <= h < 18:
                        print("  [briefing] Election day scheduled brief")
                        _send_briefing("scheduled")
                    continue

                # ── Normal days: once at schedule_time ────────────────────
                if last_scheduled_date == today:
                    continue   # already sent today
                sched = cfg.get("schedule_time", "08:00")
                try:
                    sched_h, sched_m = int(sched.split(":")[0]), int(sched.split(":")[1])
                except Exception:
                    sched_h, sched_m = 8, 0
                if now_ist.hour > sched_h or (now_ist.hour == sched_h and now_ist.minute >= sched_m):
                    # Check RISK 5: missed send catchup (only if last send > 23h ago)
                    last_sent = _last_briefing_sent_at()
                    if last_sent:
                        hours_since = (datetime.now(timezone.utc) -
                                       datetime.fromisoformat(last_sent)).total_seconds() / 3600
                        if hours_since < 23:
                            last_scheduled_date = today
                            continue
                    print(f"  [briefing] Daily brief at {sched}")
                    _send_briefing("scheduled")
                    last_scheduled_date = today

            except Exception as e:
                print(f"  [briefing] Scheduler error: {e}")
                time.sleep(60)

    threading.Thread(target=_scheduler_loop, daemon=True, name="briefing-scheduler").start()

def _save_cycle(result):
    """Persist a completed fetch cycle to the cycles table."""
    if not FEATURE_SQLITE or not result:
        return
    try:
        # result may be an Anthropic envelope — unwrap content[0].text if needed
        if "content" in result and isinstance(result.get("content"), list):
            try:
                parsed = json.loads(result["content"][0]["text"])
            except Exception:
                return
        else:
            parsed = result
        m  = parsed.get("metrics") or {}
        ts = datetime.now(timezone.utc).isoformat()
        with _db_lock:
            c = _db_conn()
            c.execute("""
                INSERT INTO cycles (fetched_at, threat_level, total_signals, high_alerts, violence_reports, full_json)
                VALUES (?,?,?,?,?,?)
            """, (
                ts,
                m.get("threatLevel", "LOW"),
                m.get("totalSignals", 0),
                m.get("highAlerts", 0),
                m.get("violenceReports", 0),
                json.dumps(parsed),
            ))
            c.commit(); c.close()
        print(f"  [cycle] Saved — threat={m.get('threatLevel','LOW')} signals={m.get('totalSignals',0)}")
    except Exception as e:
        print(f"  [cycle] Save error: {e}")


def _last_briefing_sent_at():
    """Return ISO timestamp of most recent briefing send, or None."""
    if not FEATURE_SQLITE:
        return None
    try:
        with _db_lock:
            c = _db_conn()
            row = c.execute(
                "SELECT sent_at FROM briefing_sends ORDER BY id DESC LIMIT 1"
            ).fetchone()
            c.close()
        return row["sent_at"] if row else None
    except Exception:
        return None


# ── Phase 4: Per-AC threat metrics for map overlay ───────────────────────────

# Keywords per AC — used to tag feed items to a specific constituency
_AC_KEYWORDS = {
    "SALTORA (SC)":    ["saltora"],
    "CHHATNA":         ["chhatna"],
    "RANIBANDH (ST)":  ["ranibandh", "raniband"],
    "RAIPUR (ST)":     ["raipur"],
    "TALDANGRA":       ["taldangra", "tal dangra"],
    "BANKURA":         ["bankura sadar", "bankura town", "bankura municipality",
                        "bankura constituency"],
    "BARJORA":         ["barjora", "bar jora"],
    "ONDA":            ["onda"],
    "BISHNUPUR":       ["bishnupur"],
    "KOTULPUR (SC)":   ["kotulpur", "katulpur"],
    "INDAS (SC)":      ["indas", "indus"],
    "SONAMUKHI (SC)":  ["sonamukhi", "sonamukhi"],
}

# Map AC name → figure names for mention counting
# Built dynamically from POLITICAL_FIGURES at call time


def _build_ac_metrics(panel_items, figure_mentions):
    """
    Scan all feed items and figure mentions to produce per-AC threat metrics.
    Returns dict keyed by AC_NAME matching the GeoJSON properties.
    """
    # Build AC → figure name mapping from POLITICAL_FIGURES
    ac_figures = {ac: [] for ac in _AC_KEYWORDS}
    for fig in POLITICAL_FIGURES:
        role = fig.get("role", "").upper()
        for ac in _AC_KEYWORDS:
            bare = ac.replace(" (SC)","").replace(" (ST)","")
            if bare in role or any(kw.upper() in role for kw in _AC_KEYWORDS[ac]):
                ac_figures[ac].append(fig["name"])

    # Count figure mentions per AC
    fig_mention_map = {f["name"]: f["count"] for f in figure_mentions}

    # Flatten all WB panel items
    all_items = []
    for panel in ["bankura","surrounds","statewide","alerts","official","parties","bangla"]:
        all_items.extend(panel_items.get(panel, []))

    result = {}
    for ac, kws in _AC_KEYWORDS.items():
        high_count = medium_count = 0
        for it in all_items:
            text = (it.get("title","") + " " + it.get("desc","")).lower()
            if any(kw in text for kw in kws):
                sev = _score_severity(it.get("title","") + " " + it.get("desc",""))
                if sev == "high":   high_count   += 1
                elif sev == "medium": medium_count += 1

        # Count figure mentions for this AC
        fig_count = sum(fig_mention_map.get(name, 0) for name in ac_figures.get(ac, []))

        # Compute AC-level threat
        if high_count >= 2:
            threat = "CRITICAL"
        elif high_count >= 1:
            threat = "HIGH"
        elif medium_count >= 2:
            threat = "MODERATE"
        else:
            threat = "LOW"

        result[ac] = {
            "high":    high_count,
            "medium":  medium_count,
            "figures": fig_count,
            "threat":  threat,
        }

    active = sum(1 for v in result.values() if v["threat"] != "LOW")
    print(f"  [map] AC metrics: {active}/12 ACs with elevated threat")
    return result


# ── Phase 3C: ECI Voter Turnout Tracker ──────────────────────────────────────

# Districts in WB Phase 1 (Bankura observer covers all of these)
_TURNOUT_DISTRICTS = [
    "Bankura", "Purulia", "Jhargram",
    "Paschim Bardhaman", "Paschim Medinipur", "Birbhum",
]

_turnout_cache = {"ts": 0.0, "data": {}}   # 10-min cache

def fetch_turnout_data():
    """
    Fetch voter turnout percentages for WB Phase 1 districts.

    Strategy (tries each in order, stops at first success):
    1. Scrape ECI voter turnout page (election day only)
    2. Parse turnout mentions from Google News RSS
    3. Return last known DB values if both fail
    """
    global _turnout_cache
    if time.time() - _turnout_cache["ts"] < 600:
        return _turnout_cache["data"]

    result = _scrape_eci_turnout() or _parse_turnout_from_rss()

    if result:
        _turnout_cache = {"ts": time.time(), "data": result}
        _save_turnout_snapshot(result)
    else:
        # Fall back to last DB snapshot per district
        result = _load_last_turnout_from_db()
        if result:
            _turnout_cache = {"ts": time.time(), "data": result}

    return result or {}


def _scrape_eci_turnout():
    """
    Scrape ECI voter turnout app page for WB Phase 1 data.
    Only runs on election day (23 Apr 2026) to avoid wasted requests.
    Returns {district: {"pct": float, "source": "eci_scrape"}} or None.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if today != "2026-04-23":
        return None

    urls_to_try = [
        "https://results.eci.gov.in/AcResultGenJune2024/voterturnout.htm",
        "https://www.eci.gov.in/voter-turnout",
    ]
    district_patterns = {
        "Bankura":           r"bankura[^\d]*?([\d.]+)\s*%",
        "Purulia":           r"purulia[^\d]*?([\d.]+)\s*%",
        "Jhargram":          r"jhargram[^\d]*?([\d.]+)\s*%",
        "Paschim Bardhaman": r"(?:paschim bardhaman|west burdwan)[^\d]*?([\d.]+)\s*%",
        "Paschim Medinipur": r"(?:paschim medinipur|west midnapore)[^\d]*?([\d.]+)\s*%",
        "Birbhum":           r"birbhum[^\d]*?([\d.]+)\s*%",
    }
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read(300_000).decode("utf-8", errors="replace").lower()
            found = {}
            for district, pattern in district_patterns.items():
                m = re.search(pattern, html, re.I)
                if m:
                    found[district] = {"pct": float(m.group(1)), "source": "eci_scrape"}
            if found:
                print(f"  [turnout] ECI scrape: {len(found)} districts found")
                return found
        except Exception as e:
            print(f"  [turnout] ECI scrape failed ({url}): {e}")
    return None


def _parse_turnout_from_rss():
    """
    Parse voter turnout percentages from Google News RSS headlines.
    Looks for patterns like "Bankura records 68% voter turnout".
    Returns {district: {"pct": float, "source": "rss_parse"}} or None.
    """
    RSS_TURNOUT = [
        "https://news.google.com/rss/search?q=west+bengal+voter+turnout+2026&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=bankura+purulia+voter+turnout+phase+1&hl=en-IN&gl=IN&ceid=IN:en",
    ]
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    district_kw = {
        "Bankura":           ["bankura"],
        "Purulia":           ["purulia"],
        "Jhargram":          ["jhargram"],
        "Paschim Bardhaman": ["paschim bardhaman", "west burdwan", "bardhaman"],
        "Paschim Medinipur": ["paschim medinipur", "west midnapore", "medinipur"],
        "Birbhum":           ["birbhum"],
    }
    pct_pattern = re.compile(r'([\d.]+)\s*(?:%|per\s*cent)', re.I)
    found = {}
    for rss_url in RSS_TURNOUT:
        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=12) as resp:
                xml_raw = resp.read(200_000)
            root = ET.fromstring(xml_raw)
            items = root.findall(".//item")
            for item in items:
                title = (item.findtext("title") or "").lower()
                desc  = (item.findtext("description") or "").lower()
                text  = title + " " + desc
                if not any(kw in text for kw in ["turnout", "voter turn", "polled", "voted"]):
                    continue
                for district, keywords in district_kw.items():
                    if district in found:
                        continue
                    if any(kw in text for kw in keywords):
                        m = pct_pattern.search(text)
                        if m:
                            pct = float(m.group(1))
                            if 0 < pct <= 100:
                                found[district] = {"pct": pct, "source": "rss_parse"}
        except Exception as e:
            print(f"  [turnout] RSS parse error: {e}")
    if found:
        print(f"  [turnout] RSS parse: {len(found)} districts found")
        return found or None
    return None


def _load_last_turnout_from_db():
    """Return the most recent turnout snapshot per district from DB."""
    if not FEATURE_SQLITE:
        return {}
    try:
        with _db_lock:
            c = _db_conn()
            rows = c.execute("""
                SELECT district, turnout_pct, booths_total, booths_reported,
                       source, recorded_at
                FROM turnout_snapshots
                WHERE id IN (
                    SELECT MAX(id) FROM turnout_snapshots GROUP BY district
                )
            """).fetchall()
            c.close()
        result = {}
        for row in rows:
            result[row["district"]] = {
                "pct":              row["turnout_pct"],
                "booths_total":     row["booths_total"],
                "booths_reported":  row["booths_reported"],
                "source":           row["source"] or "db_cache",
                "recorded_at":      row["recorded_at"],
            }
        return result
    except Exception:
        return {}


def _save_turnout_snapshot(data):
    """Persist a turnout data dict to DB. data = {district: {pct, booths_total?, ...}}"""
    if not FEATURE_SQLITE or not data:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        c = _db_conn()
        for district, vals in data.items():
            c.execute("""
                INSERT INTO turnout_snapshots
                  (recorded_at, district, turnout_pct, booths_total, booths_reported, source)
                VALUES (?,?,?,?,?,?)
            """, (
                now, district,
                vals.get("pct"),
                vals.get("booths_total"),
                vals.get("booths_reported"),
                vals.get("source", "unknown"),
            ))
        c.commit(); c.close()
    print(f"  [turnout] Saved {len(data)} district snapshot(s) to DB")


def _save_manual_turnout(district, pct, booths_total=None, booths_reported=None):
    """Save a manually-entered turnout value. Returns the updated full snapshot."""
    entry = {
        "pct":             float(pct),
        "booths_total":    int(booths_total)    if booths_total    else None,
        "booths_reported": int(booths_reported) if booths_reported else None,
        "source":          "manual",
    }
    _save_turnout_snapshot({district: entry})
    # Invalidate cache so next fetch returns fresh DB values
    global _turnout_cache
    _turnout_cache["ts"] = 0.0
    return _load_last_turnout_from_db()


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # required for Transfer-Encoding: chunked (Chrome strict)
    backend    = "ollama"
    api_key    = ""
    model      = ""
    backend_url = ""

    def log_message(self, fmt, *args):
        print("  " + time.strftime("%H:%M:%S") + "  " + (fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
            "Content-Type, x-api-key, anthropic-version, anthropic-beta")
        self.send_header("Connection", "close")   # disable HTTP/1.1 keep-alive (we don't multiplex)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        file_map = {
            "/":                              DASHBOARD_FILE,
            "/dashboard":                     DASHBOARD_FILE,
            "/wb_live_intel_dashboard.html":  DASHBOARD_FILE,
            "/osint":                         OSINT_FILE,
            "/wb_osint_monitor.html":         OSINT_FILE,
        }
        if path == "/status":
            body = json.dumps({
                "status":   "ok",
                "backend":  self.backend,
                "model":    self.model,
                "key_set":  bool(self.api_key),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return
        target = file_map.get(path)
        if target and target.exists():
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        elif path == "/social-links":
            body = json.dumps(SOCIAL_MONITOR_LINKS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path == "/api/digests":
            # GET /api/digests?days=7&sev=all|high|medium&status=all|done|pending
            if not FEATURE_SQLITE:
                body = json.dumps({"articles":[],"stats":{}}).encode()
            else:
                qs     = parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
                days   = int(qs.get("days",  ["7"])[0])
                sev    = qs.get("sev",    ["all"])[0]
                status = qs.get("status", ["all"])[0]
                cutoff = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() - days * 86400,
                    tz=timezone.utc
                ).isoformat()
                with _db_lock:
                    c = _db_conn()
                    where  = ["first_seen >= ?"];  args = [cutoff]
                    if sev    != "all": where.append("severity=?");      args.append(sev)
                    if status != "all": where.append("digest_status=?"); args.append(status)
                    sql = ("SELECT id,url,title,source,severity,panel,"
                           "published_at,first_seen,digest,digest_status,digested_at "
                           "FROM digested_articles WHERE " + " AND ".join(where) +
                           " ORDER BY CASE severity WHEN 'high' THEN 1 ELSE 2 END,"
                           " CASE panel WHEN 'bankura' THEN 1 WHEN 'alerts' THEN 2"
                           "            WHEN 'surrounds' THEN 3 ELSE 4 END,"
                           " first_seen DESC LIMIT 300")
                    rows   = [dict(r) for r in c.execute(sql, args).fetchall()]
                    stats  = dict(c.execute("""
                        SELECT digest_status, COUNT(*) as n
                        FROM digested_articles WHERE first_seen >= ?
                        GROUP BY digest_status
                    """, [cutoff]).fetchall() or [])
                    c.close()
                body = json.dumps({"articles": rows, "stats": stats}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path == "/api/wire":
            # GET /api/wire — ANI + PTI West Bengal wire feed (cached 10 min)
            try:
                items = _fetch_wire()
            except Exception as e:
                print(f"  [wire] ERROR in _fetch_wire: {e}")
                items = []
            body = json.dumps({"items": items, "count": len(items)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path == "/api/figures-news":
            # GET /api/figures-news — per-candidate Google News (cached 15 min)
            try:
                data = _fetch_figures_news()
            except Exception as e:
                print(f"  [figures-news] ERROR: {e}")
                data = {}
            body = json.dumps({"figures": data, "count": len(data)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path.startswith("/geojson/"):
            # GET /geojson/<filename> — serve static GeoJSON files
            fname = path[len("/geojson/"):]
            # Safety: only allow simple filenames, no path traversal
            if "/" in fname or ".." in fname:
                self.send_error(400); return
            geo_path = SCRIPT_DIR / "geojson" / fname
            if geo_path.exists() and geo_path.suffix == ".geojson":
                content = geo_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/geo+json")
                self._cors(); self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)

        elif path == "/api/turnout":
            # GET /api/turnout — latest voter turnout per district
            try:
                data = fetch_turnout_data()
            except Exception as e:
                print(f"  [turnout] fetch error: {e}")
                data = {}
            # Also return history for trend chart (last 20 snapshots for Bankura)
            history = []
            if FEATURE_SQLITE:
                try:
                    with _db_lock:
                        c = _db_conn()
                        rows = c.execute("""
                            SELECT recorded_at, district, turnout_pct
                            FROM turnout_snapshots
                            WHERE district='Bankura'
                            ORDER BY recorded_at DESC LIMIT 20
                        """).fetchall()
                        c.close()
                    history = [{"ts": r["recorded_at"], "pct": r["turnout_pct"]}
                               for r in reversed(rows)]
                except Exception:
                    pass
            body = json.dumps({
                "districts": data,
                "history":   history,
                "districts_list": _TURNOUT_DISTRICTS,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path == "/api/briefing/config":
            # GET /api/briefing/config — return current config (mask sensitive keys)
            cfg = _load_briefing_config()
            safe = dict(cfg)
            if safe.get("telegram_bot_token"):
                safe["telegram_bot_token"] = "***"   # never expose token to browser
            if safe.get("sms_api_key"):
                safe["sms_api_key"] = "***"
            body = json.dumps(safe).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path == "/api/briefing/send":
            # GET /api/briefing/send — trigger briefing immediately
            result = _send_briefing("manual")
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path == "/api/telegram/chatid":
            # GET /api/telegram/chatid?token=... — auto-detect chat_id from getUpdates
            token = ""
            if "?" in self.path:
                token = parse_qs(self.path.split("?",1)[1]).get("token",[""])[0].strip()
            if not token:
                # fall back to saved token
                token = _load_briefing_config().get("telegram_bot_token","").strip()
            if not token:
                self._reply(400, json.dumps({"error": "bot token required"}).encode())
                return
            try:
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                req = urllib.request.Request(url, headers={"User-Agent": "WBIntelProxy/1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                if not data.get("ok"):
                    self._reply(502, json.dumps({"error": data.get("description","Telegram error")}).encode())
                    return
                results = data.get("result", [])
                if not results:
                    self._reply(200, json.dumps({
                        "chat_id": None,
                        "hint": "No messages found — send /start to your bot first, then retry"
                    }).encode())
                    return
                # Pick the most recent chat_id
                msg = results[-1].get("message") or results[-1].get("channel_post") or {}
                chat_id = str((msg.get("chat") or {}).get("id",""))
                chat_title = (msg.get("chat") or {}).get("title") or (msg.get("from") or {}).get("first_name","")
                self._reply(200, json.dumps({"chat_id": chat_id, "chat_title": chat_title}).encode())
            except Exception as e:
                self._reply(502, json.dumps({"error": str(e)[:200]}).encode())

        elif path == "/api/briefing/history":
            # GET /api/briefing/history?limit=20
            limit = 20
            if "?" in self.path:
                qs = parse_qs(self.path.split("?",1)[1])
                limit = int(qs.get("limit",["20"])[0])
            rows = []
            if FEATURE_SQLITE:
                try:
                    with _db_lock:
                        c = _db_conn()
                        rows = [dict(r) for r in c.execute("""
                            SELECT id, sent_at, trigger, threat_level, headline,
                                   ntfy_status, sms_status, cycle_id
                            FROM briefing_sends ORDER BY id DESC LIMIT ?
                        """, (limit,)).fetchall()]
                        c.close()
                except Exception as e:
                    print(f"  [briefing] history error: {e}")
            body = json.dumps({"sends": rows}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        elif path == "/api/digest/redigest":
            # POST-like GET: /api/digest/redigest?id=N  — re-queue a failed/done article
            if FEATURE_SQLITE and "?" in self.path:
                aid = int(parse_qs(self.path.split("?",1)[1]).get("id",["0"])[0])
                if aid:
                    with _db_lock:
                        c = _db_conn()
                        c.execute("UPDATE digested_articles SET digest_status='pending' WHERE id=?", (aid,))
                        c.commit(); c.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(b'{"ok":true}')

        else:
            self.send_error(404)

    def _reply(self, status, body_bytes):
        """Send a simple fixed-length response (for errors / fast replies)."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self._cors()
            self.end_headers()
            self.wfile.write(body_bytes)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _reply_chunked(self, compute_fn):
        """
        Chunked-transfer response with 10-second keepalive newlines.

        Solves Safari's ~90-second idle-TCP timeout on localhost:
        while the LLM (especially large models like qwen3.5:9b) is thinking,
        this method sends a tiny whitespace chunk every 10 seconds so Safari
        sees data flowing and keeps the connection open.

        The dashboard's repairJSON / JSON.parse handles leading whitespace fine.
        """
        result_box = [None]
        error_box  = [None]
        done       = threading.Event()

        def _worker():
            try:
                result_box[0] = compute_fn()
            except Exception as exc:
                error_box[0] = exc
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()

            def send_chunk(data: bytes):
                self.wfile.write(f"{len(data):X}\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            # Send a newline keepalive every 10 s while LLM is working
            while not done.wait(timeout=10):
                send_chunk(b"\n")

            if error_box[0]:
                raise error_box[0]

            # Send the real JSON body
            body = json.dumps(result_box[0]).encode()
            send_chunk(body)
            # Chunked-encoding terminator
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected — normal for browser timeout

    def do_POST(self):
        if self.path == "/api/briefing/config":
            # POST /api/briefing/config — save config {enabled, telegram_bot_token, ...}
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length))
                allowed = set(_BRIEFING_DEFAULTS.keys())
                updates = {k: v for k, v in body.items() if k in allowed}
                _save_briefing_config(updates)
                self._reply(200, json.dumps({"ok": True}).encode())
            except Exception as e:
                self._reply(500, json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/api/turnout":
            # POST /api/turnout — manual entry: {district, pct, booths_total?, booths_reported?}
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length))
                district = body.get("district", "").strip()
                pct      = float(body.get("pct", 0))
                if district not in _TURNOUT_DISTRICTS:
                    self._reply(400, json.dumps({"error": "unknown district"}).encode())
                    return
                if not (0 <= pct <= 100):
                    self._reply(400, json.dumps({"error": "pct out of range"}).encode())
                    return
                updated = _save_manual_turnout(
                    district, pct,
                    body.get("booths_total"),
                    body.get("booths_reported"),
                )
                self._reply(200, json.dumps({"ok": True, "districts": updated}).encode())
            except Exception as e:
                self._reply(500, json.dumps({"error": str(e)}).encode())
            return

        if self.path != "/v1/messages":
            self.send_error(404)
            return

        try:
            length  = int(self.headers.get("Content-Length", 0))
            raw     = self.rfile.read(length)
            payload = json.loads(raw)
        except Exception as e:
            self._reply(400, json.dumps({"error": {"message": "Bad request: " + str(e)}}).encode())
            return

        # Inject live RSS news into prompt (replaces web_search tool)
        pre_built = None
        try:
            payload = inject_news_into_payload(payload)
            pre_built = payload.pop("_pre_built", None)
        except Exception as e:
            print(f"  RSS fetch error: {e}")

        def compute():
            b = self.backend
            if b == "ollama":
                result = call_ollama(payload, self.backend_url, self.model)
            elif b in ("groq", "openai", "openai-compat"):
                result = call_openai_compat(payload, self.backend_url, self.api_key, self.model)
            elif b == "gemini":
                result = call_gemini(payload, self.api_key, self.model)
            else:
                raise ValueError("Unknown backend: " + b)
            if pre_built:
                result = merge_prebuilt_with_analysis(result, pre_built)
            # Persist completed cycle to DB for briefing + history
            _save_cycle(result)
            return result

        try:
            self._reply_chunked(compute)

        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"  HTTP {e.code} from backend: {err_body[:200]}")
            self._reply(200, json.dumps({
                "error": {
                    "type":    "backend_error",
                    "message": f"Backend HTTP {e.code}: {err_body.decode('utf-8','replace')[:300]}"
                }
            }).encode())
        except (BrokenPipeError, ConnectionResetError):
            print("  Client disconnected before response was sent (normal — browser timeout)")
        except Exception as e:
            msg = str(e)
            print(f"  Backend error: {msg}")
            self._reply(200, json.dumps({
                "error": {
                    "type":    "proxy_error",
                    "message": msg
                }
            }).encode())

class QuietHandler:
    """Mixin that suppresses BrokenPipe tracebacks from socketserver."""
    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            pass  # normal — client closed connection
        else:
            super().handle_error(request, client_address)

class QuietHTTPServer(QuietHandler, socketserver.ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server — each request handled in its own thread.
    This prevents long LLM requests (40-120s) from blocking the /status endpoint
    and subsequent dashboard fetches."""
    daemon_threads = True   # threads die with the server process

def find_free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("No free port")

def main():
    parser = argparse.ArgumentParser(description="WB Election Intel — Local LLM proxy")
    parser.add_argument("--backend", default="ollama",
        choices=["ollama","groq","gemini","openai","openai-compat"],
        help="Which LLM backend to use (default: ollama)")
    parser.add_argument("--key",   default="", help="API key (Groq/Gemini/OpenAI)")
    parser.add_argument("--model", default="",
        help="Model name (default: qwen2.5:7b for Ollama, llama-3.3-70b-versatile for Groq, gemini-1.5-flash for Gemini)")
    parser.add_argument("--url",   default="",
        help="Backend base URL (default: http://localhost:11434 for Ollama)")
    parser.add_argument("--port",  type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # Set defaults per backend
    defaults = {
        "ollama":       ("http://localhost:11434", "qwen2.5:7b"),
        "groq":         ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        "gemini":       ("", "gemini-1.5-flash"),
        "openai":       ("https://api.openai.com/v1", "gpt-4o-mini"),
        "openai-compat":("http://localhost:11434/v1", "qwen2.5:7b"),
    }
    default_url, default_model = defaults.get(args.backend, ("", ""))
    backend_url = args.url   or default_url

    # If no model specified and backend is ollama, offer interactive selection
    if not args.model and args.backend == "ollama":
        try:
            chk = urllib.request.urlopen(
                default_url.rstrip("/") + "/api/tags", timeout=4
            )
            tags_data = json.loads(chk.read())
            installed = [m["name"] for m in tags_data.get("models", [])]
        except Exception:
            installed = []

        if installed:
            print("  Installed Ollama models:")
            for i, name in enumerate(installed, 1):
                print(f"    {i}) {name}")
            print()
            try:
                raw = input(f"  Select model [1-{len(installed)}, default 1]: ").strip()
            except (EOFError, KeyboardInterrupt):
                raw = "1"
            if raw.isdigit() and 1 <= int(raw) <= len(installed):
                model = installed[int(raw) - 1]
            elif raw and not raw.isdigit():
                model = raw          # typed a model name directly
            else:
                model = installed[0] if installed else default_model
        else:
            model = default_model
    else:
        model = args.model or default_model
    api_key     = args.key   or os.environ.get("GROQ_API_KEY","") or \
                               os.environ.get("GEMINI_API_KEY","") or \
                               os.environ.get("OPENAI_API_KEY","")

    Handler.backend     = args.backend
    Handler.api_key     = api_key
    Handler.model       = model
    Handler.backend_url = backend_url

    port = find_free_port(args.port)

    print("\n  ╔══════════════════════════════════════════════════════════╗")
    print("  ║   WB ELECTION INTEL — Local LLM Proxy                   ║")
    print("  ╚══════════════════════════════════════════════════════════╝\n")
    # Show available local models if Ollama
    if args.backend == "ollama":
        import subprocess
        try:
            result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip() and not l.startswith("NAME")]
                if lines:
                    print("  Installed models:")
                    for l in lines:
                        name = l.split()[0]
                        marker = " <-- selected" if name.startswith(model.split(":")[0]) else ""
                        print(f"    {name}{marker}")
                    print()
        except Exception:
            pass
    # ── Ollama connectivity check ──────────────────────────────────────
    if args.backend == "ollama":
        print("  Checking Ollama connection...")
        try:
            chk = urllib.request.urlopen(
                backend_url.rstrip("/") + "/api/tags", timeout=4
            )
            tags_data = json.loads(chk.read())
            installed = [m["name"] for m in tags_data.get("models", [])]
            if installed:
                print("  Ollama OK. Installed models:")
                for m_name in installed:
                    marker = " <-- will use" if m_name.startswith(model.split(":")[0]) else ""
                    print(f"    {m_name}{marker}")
                if not any(m_name.startswith(model.split(":")[0]) for m_name in installed):
                    print(f"\n  WARNING: '{model}' not found in installed models above.")
                    print(f"  Fix:  ollama pull {model}")
                    print(f"  Or restart proxy with one of the installed model names above.\n")
            else:
                print("  Ollama running but no models installed.")
                print(f"  Run:  ollama pull {model}")
        except Exception as e:
            print(f"  WARNING: Cannot reach Ollama at {backend_url}")
            print(f"  Error: {e}")
            print(f"  Fix:  run 'ollama serve' in another terminal, then retry.")
        print()
    # ───────────────────────────────────────────────────────────────────
    print(f"  Backend   :  {args.backend.upper()}")
    print(f"  Model     :  {model}")
    if backend_url:
        print(f"  URL       :  {backend_url}")
    if api_key:
        print(f"  API key   :  {api_key[:16]}...")
    print(f"  Dashboard :  http://localhost:{port}/")
    print(f"  OSINT     :  http://localhost:{port}/osint")
    print(f"\n  News source: Live RSS feeds (Google News, no API key needed)")
    print(f"\n  Press Ctrl+C to stop\n")

    # ── Phase 2: init SQLite DB and start background digest worker ───────────
    _db_init()
    _start_digest_worker(ollama_base=f"http://localhost:11434", model=model)

    # ── Phase 4B: start daily briefing scheduler ──────────────────────────────
    _start_briefing_scheduler()

    # Pre-warm wire cache AND keep it warm with a background refresh thread.
    # Wire cache TTL is 10 min; we refresh every 9 min so page loads always
    # get an instant cached response instead of waiting 15-30s for a cold fetch.
    def _wire_refresh_loop():
        import time as _t
        # Initial prewarm on startup
        try:
            _fetch_wire()
            print("  [wire] Cache pre-warmed")
        except Exception as e:
            print(f"  [wire] Pre-warm failed: {e}")
        # Then keep refreshing every 9 min (ahead of the 10-min TTL)
        while True:
            _t.sleep(9 * 60)
            try:
                global _wire_cache
                _wire_cache["ts"] = 0.0   # force expiry so _fetch_wire re-fetches
                _fetch_wire()
                print("  [wire] Background cache refreshed")
            except Exception as e:
                print(f"  [wire] Background refresh failed: {e}")
    threading.Thread(target=_wire_refresh_loop, daemon=True, name="wire-refresh").start()

    # Phase 3B: pre-warm figures-news cache, then refresh every 14 min (TTL=15 min)
    def _figures_refresh_loop():
        import time as _t
        try:
            _fetch_figures_news()
            print("  [figures-news] Cache pre-warmed")
        except Exception as e:
            print(f"  [figures-news] Pre-warm failed: {e}")
        while True:
            _t.sleep(14 * 60)
            try:
                global _FIGURES_NEWS_CACHE
                _FIGURES_NEWS_CACHE["ts"] = 0.0
                _fetch_figures_news()
                print("  [figures-news] Background cache refreshed")
            except Exception as e:
                print(f"  [figures-news] Background refresh failed: {e}")
    threading.Thread(target=_figures_refresh_loop, daemon=True, name="figures-refresh").start()


    server = QuietHTTPServer(("127.0.0.1", port), Handler)

    if not args.no_browser:
        threading.Timer(0.9, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")

if __name__ == "__main__":
    main()
