# WB Election Intel — Architecture & Workflow

## System Overview

A self-hosted, local-first election intelligence platform. An Ollama LLM running on the operator's laptop ingests multi-source RSS news every 15 minutes, geo-filters and deduplicates it, produces a structured JSON intelligence assessment, and serves it to any browser — including mobile — via a Cloudflare tunnel. No cloud APIs, no third-party AI services, no subscription required.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        OPERATOR'S LAPTOP                            │
│                                                                     │
│  ┌─────────────┐    ┌──────────────────┐    ┌────────────────────┐ │
│  │  Google     │    │  proxy_local.py  │    │   Ollama (local)   │ │
│  │  News RSS   │───▶│  Python HTTP     │───▶│   qwen2.5:7b       │ │
│  │  (30+ feeds)│    │  Server :5050    │◀───│   (VRAM-resident)  │ │
│  └─────────────┘    └────────┬─────────┘    └────────────────────┘ │
│                              │                                      │
│  ┌─────────────┐            │ read/write                           │
│  │  ANI / PTI  │            ▼                                      │
│  │  Wire feeds │    ┌──────────────────┐                           │
│  └──────┬──────┘    │  bankura_intel   │                           │
│         └──────────▶│  .db  (SQLite)   │                           │
│                     │  - cycles        │                           │
│                     │  - digested_     │                           │
│                     │    articles      │                           │
│                     │  - turnout_      │                           │
│                     │    snapshots     │                           │
│                     │  - briefing_     │                           │
│                     │    sends         │                           │
│                     └──────────────────┘                           │
│                                                                     │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  localhost:5050
                            ▼
              ┌─────────────────────────┐
              │   Cloudflare Tunnel     │
              │   cloudflared daemon    │
              │   (LaunchAgent,         │
              │    auto-restart)        │
              └────────────┬────────────┘
                           │  HTTPS
                           ▼
              ┌─────────────────────────┐
              │  elections-live         │
              │  .observer              │
              │  (public domain)        │
              └────────────┬────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
              ▼                         ▼
   ┌──────────────────┐      ┌──────────────────┐
   │  Desktop Browser │      │  Mobile Browser  │
   │  (full layout)   │      │  (responsive UI) │
   └──────────────────┘      └──────────────────┘
```

---

## Data Pipeline — Fetch Cycle (every 15 min)

```
Step 1: RSS FETCH
─────────────────
  30+ Google News queries across 7 panel categories:
  ┌──────────────┬────────────────────────────────────────────────┐
  │ Panel        │ Content                                        │
  ├──────────────┼────────────────────────────────────────────────┤
  │ bankura      │ 8 feeds — Bankura ACs, local candidates, MCC  │
  │ surrounds    │ 6 feeds — Purulia, Jhargram, Birbhum,         │
  │              │           Bardhaman, Medinipur                 │
  │ statewide    │ 8 feeds — WB-wide: NDTV, Telegraph, ANI       │
  │ alerts       │ 6 feeds — Violence, booth capture, EVM, arms  │
  │ official     │ 4 feeds — ECI orders, CRPF, CEO West Bengal   │
  │ parties      │ 4 feeds — BJP/TMC/CPM/INC press releases      │
  │ bangla       │ 4 feeds — Bengali media (Anandabazar, Eisamay)│
  └──────────────┴────────────────────────────────────────────────┘
  All feeds use when="2d" (48-hour rolling window)

Step 2: GEO-FILTER (per panel)
───────────────────────────────
  _is_wb_relevant(item):
    ✓ PASS  — contains WB district/city/party name keyword
    ✗ DROP  — contains: manipur, chhattisgarh, israel, ipl,
                        cricket, stock market, trump, nato …

Step 3: ELECTION RELEVANCE FILTER
───────────────────────────────────
  _is_election_relevant(item):
    ✓ PASS  — contains: election, vote, candidate, booth,
                        evm, crpf, nomination, rally …
    ✗ DROP  — generic news with no election context

Step 4: TITLE DEDUPLICATION
─────────────────────────────
  fingerprint = alphanumeric(title)[:40]
  Skip if fingerprint already seen in this cycle

Step 5: INCIDENT DEDUPLICATION (violence counting)
────────────────────────────────────────────────────
  _count_violence_incidents():
    For each article with violence keyword:
      fp = "location_keyword__violence_keyword"
      e.g. "malda__violence" — 9 articles → 1 incident
    Returns: count of unique incident fingerprints

Step 6: METRICS CALCULATION
─────────────────────────────
  violence_count  = unique (location, violence_type) pairs
  mcc_count       = kw_count(wb_raw_dedup, mcc_keywords)
  high_items      = LLM-graded HIGH severity count
  ┌──────────────────────────────────────────────────────┐
  │  CRITICAL  ≥ 5 distinct violence incidents           │
  │  HIGH      ≥ 2 distinct violence incidents           │
  │  MODERATE  ≥ 1 incident OR 3+ MCC                   │
  │  LOW       no confirmed incidents                    │
  └──────────────────────────────────────────────────────┘

Step 7: LLM ANALYSIS (qwen2.5:7b via Ollama)
──────────────────────────────────────────────
  Input:  Structured panel brief (headlines + summaries)
  Output: JSON
  {
    "analysis": {
      "headline": "...",
      "bankuraSituation": "...",
      "statewideContext": "...",
      "keyDevelopments": [...],
      "observerActions": [...],
      "disinfoAlerts": "...",
      "overallAssessment": "..."
    }
  }

Step 8: PERSIST TO SQLite
───────────────────────────
  INSERT INTO cycles (fetched_at, threat_level,
    total_signals, high_alerts, violence_reports, full_json)

Step 9: BACKGROUND ARTICLE DIGEST
────────────────────────────────────
  For each MED/HIGH article not yet digested:
    → LLM generates 2-3 sentence summary
    → Stored in digested_articles table
    → Surfaced in Night Digest modal
```

---

## HTTP Server — API Endpoints

```
GET  /                        → wb_live_intel_dashboard.html
GET  /api/latest              → Latest cycle JSON from DB (cached)
GET  /api/trend?limit=N       → Lightweight trend points for chart
GET  /api/cycles?limit=N      → Full cycle list for timeline modal
GET  /api/digests             → Article digest list (filter: days, sev)
GET  /api/wire                → ANI + PTI wire feed (10-min cache)
GET  /api/figures-news        → Per-candidate news (15-min cache)
GET  /api/turnout             → Turnout snapshots from DB
GET  /api/briefing/config     → Telegram briefing config
GET  /api/briefing/history    → Past briefing send log
GET  /api/briefing/send       → Trigger scheduled briefing check

POST /api/fetch/trigger       → Force a new fetch cycle
POST /api/briefing/config     → Save briefing config (token, chat_id)
POST /api/turnout             → Submit manual turnout snapshot
POST /api/digest/redigest     → Re-queue a failed digest article
```

---

## Frontend Architecture

```
wb_live_intel_dashboard.html  (single-file SPA, ~4000 lines)
│
├── Dependencies
│   ├── Chart.js 4.4.0        — Trend sparkline
│   └── Leaflet 1.9.4         — AC constituency map
│
├── Layout (Desktop)
│   ├── Topbar                — Title, live status, controls
│   ├── Sidebar               — Controls, topics, digest, Telegram
│   └── Main content
│       ├── Alert banner      — HIGH/CRITICAL escalation strip
│       ├── Metrics row       — 4 KPI cards (signals/alerts/viol/threat)
│       ├── Turnout section   — Election day turnout table + chart
│       ├── Map panel         — Leaflet AC map (Bankura 9 ACs)
│       ├── Intel grid        — 3-col: Bankura | Statewide | Wire
│       ├── Mid row
│       │   ├── Trend panel   — Chart.js sparkline (threat + violence)
│       │   └── Figures panel — Political figures monitor
│       └── Feeds grid        — 7 news feed panels
│
├── Layout (Mobile ≤768px)
│   ├── Sticky topbar         — Single line, hamburger menu
│   ├── Sidebar drawer        — Slide-in from left (64vw)
│   ├── Ticker strip          — Horizontal scrolling alerts
│   ├── Metrics row           — Compact KPI cards
│   ├── Trend panel           — Compact sparkline
│   ├── Priority feed         — Unified HIGH→MED→LOW (10 items)
│   └── Figures panel         — Political figures below feed
│
├── Data flow (client side)
│   ├── On load: GET /api/latest → applyParsedCycle()
│   ├── Every 60s: poll /api/latest → update if new cycle
│   ├── On load: GET /api/trend → seed localStorage + render chart
│   └── saveTrendPoint(): append each new cycle to localStorage
│
└── Key JS modules
    ├── renderTrendChart()    — Chart.js sparkline with sparse X labels
    ├── buildMobileFeed()     — Priority-sorted unified mobile feed
    ├── updateMobTicker()     — Horizontal auto-scroll ticker
    ├── _recalibrateTrend()   — Corrects pre-fix CRITICAL data points
    ├── renderFigures()       — Political figures news cards
    └── _renderTimeline()     — Cycle history modal
```

---

## Deployment Architecture

```
┌─────────────────────────────────────────────────────┐
│  macOS (M-series)                                   │
│                                                     │
│  LaunchAgent: com.cloudflare.cloudflared.wb-elections
│    → Runs cloudflared at login, auto-restarts       │
│    → Logs: /tmp/cloudflared-wb.log                  │
│                                                     │
│  Ollama: qwen2.5:7b loaded in VRAM                  │
│    → Evicts competing models before each fetch      │
│    → Inference: ~30-60s per fetch cycle             │
│                                                     │
│  proxy_local.py (prod): port 5050                   │
│  proxy_local.py (dev):  port 5055                   │
│                                                     │
│  bankura_intel.db (SQLite)                          │
│    → Shared between proxy + clients via /api/latest │
│    → Single _db_lock for thread-safe writes         │
│    → _fetch_lock prevents concurrent LLM calls      │
└─────────────────────────────────────────────────────┘

Dev / Prod separation:
  wb_election_intel_dev/   → port 5055, dev branch
  wb_election_intel/       → port 5050, master branch
  All changes: dev first → verify → sync to prod
```

---

## Security Model

```
Credential handling:
  telegram_config.json  → chmod 600, gitignored
  .env (MTProto creds)  → chmod 600, gitignored
  *.session             → chmod 600, gitignored
  cloudflared/*.json    → chmod 600, outside repos

Git repositories (GitHub):
  No credentials ever committed
  No DB files committed (*.db gitignored)
  No logs committed (*.log gitignored)

Network:
  Cloudflare tunnel: HTTPS termination at edge
  Local proxy: HTTP on localhost only
  All /api/* calls use window.location.origin
    → works over both localhost and tunnel HTTPS
```
