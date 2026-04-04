# WB Election Intel

**Real-time election intelligence dashboard for field observers — West Bengal Assembly Elections 2026**

A self-hosted, local-first OSINT platform that continuously monitors multi-source news, runs local LLM analysis, and delivers a structured intelligence picture to field observers on any device — including mobile — via a secure public URL. No cloud AI, no subscriptions, no data leaving your machine except through a Cloudflare tunnel you control.

---

## What it does

Every 15 minutes, the system:

1. Fetches 30+ Google News RSS feeds across 7 thematic panels (Bankura district, surrounding districts, statewide WB, alerts & MCC violations, official ECI updates, party statements, Bengali-language media)
2. Geo-filters and deduplicates articles — stripping non-WB content, removing the same incident reported by multiple sources
3. Runs the filtered headlines through a locally hosted LLM (`qwen2.5:7b` via Ollama) to produce a structured JSON intelligence assessment
4. Persists the cycle to a local SQLite database
5. Serves the latest assessment to all connected clients — browser, mobile, or any HTTP client — via a Cloudflare tunnel

The result is a continuously updated intelligence dashboard showing threat level, violence incident count, key developments, observer action recommendations, and a historical trend — all computed locally and displayed cleanly on both desktop and mobile.

---

## Dashboard features

| Feature | Description |
|---|---|
| **Live threat level** | CRITICAL / HIGH / MODERATE / LOW — computed from unique violence incident count, not raw article volume |
| **Signal metrics** | Total WB-relevant signals, high-priority alerts, violence/incident reports — updated every cycle |
| **LLM analysis** | Bankura situation brief, statewide context, key developments, observer action checklist, disinfo alerts |
| **AC constituency map** | Interactive Leaflet map of all 9 Bankura assembly constituencies with candidate overlays |
| **Political figures monitor** | Per-candidate news tracking for all key Bankura candidates and district leaders |
| **Threat & signal trend** | Chart.js sparkline across the full observation period — start / mid / current date on X axis |
| **7 news feed panels** | Bankura, Surrounding districts, WB statewide, Alerts, Official/ECI, Party statements, Bengali media |
| **Wire tracker** | ANI + PTI West Bengal wire feed, 10-minute cache |
| **Night digest** | AI-summarised MED/HIGH priority articles for end-of-day review, with unread badge |
| **Intelligence timeline** | Searchable cycle-by-cycle record with threat level, analysis, and feed snapshots |
| **Push alerts** | Browser notifications on threat level escalation |
| **Turnout dashboard** | Live turnout entry and chart (election day mode) |
| **Mobile-first layout** | Adaptive single-column layout, hamburger sidebar, horizontal ticker, priority feed (HIGH→MED→LOW) |
| **Video export** | Animated HTML slideshow of all stored intelligence cycles |

---

## Architecture

```
Google News RSS (30+ feeds)
        │
        ▼
proxy_local.py  ──── Geo-filter ──── Dedup ──── Incident fingerprint
        │
        ├──▶  Ollama (qwen2.5:7b, local VRAM)
        │          └── Structured JSON assessment
        │
        ├──▶  SQLite (bankura_intel.db)
        │          ├── cycles
        │          ├── digested_articles
        │          └── turnout_snapshots
        │
        └──▶  HTTP :5050
                   │
                   └──▶  Cloudflare Tunnel (HTTPS)
                                │
                         elections-live.observer
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
             Desktop Browser          Mobile Browser
```

**Key design decisions:**

- **Central fetch, many readers** — One LLM cycle runs every 15 min server-side; all clients read from `/api/latest` (SQLite-backed). No per-client LLM calls.
- **Incident-level deduplication** — Violence is counted by unique `(location, violence_type)` fingerprints, not article count. Nine outlets reporting the same Malda incident = 1 incident.
- **Fully local LLM** — `qwen2.5:7b` via Ollama. No OpenAI, Groq, or Gemini required (though all are supported as fallback backends).
- **Single HTML file frontend** — The entire dashboard is one self-contained HTML file served by the Python proxy. No build step, no node_modules, no framework.
- **Dev/prod isolation** — Dev instance on port 5055, prod on 5050. All changes land in dev first, then synced to prod after verification.

---

## Stack

| Component | Technology |
|---|---|
| Backend proxy | Python 3.9 — stdlib only (`http.server`, `sqlite3`, `urllib`) |
| LLM inference | Ollama — `qwen2.5:7b` (local, VRAM-resident) |
| Database | SQLite 3 |
| Frontend | Vanilla HTML/CSS/JS — no framework |
| Charts | Chart.js 4.4.0 |
| Maps | Leaflet 1.9.4 |
| Public tunnel | Cloudflare Tunnel (`cloudflared`) |
| Auto-restart | macOS LaunchAgent (plist) |
| News sources | Google News RSS (GNews URL scheme) |

---

## Intelligence pipeline detail

### Geo-filtering
Articles are accepted only if they contain West Bengal geographic keywords (district names, Kolkata, Mamata, TMC, etc.) and are rejected if they match a hard blocklist of unrelated topics: Manipur, Chhattisgarh, international news, IPL, stock market, Bollywood.

### Violence deduplication
The old approach counted keyword hits in articles — so 9 outlets covering the same incident = 9 violence "reports." The current approach fingerprints each incident as `location__violence_type` (e.g. `malda__violence`) and counts unique fingerprints. This prevents a single widely-covered incident from inflating the threat level.

### Threat calibration
```
CRITICAL  → 5+ distinct violence incidents
HIGH      → 2–4 distinct violence incidents
MODERATE  → 1 incident OR 3+ MCC violations
LOW       → No confirmed incidents
```

### RSS feed windows
All feeds use a 48-hour rolling window (`when=2d`). This keeps signal count at ~50–70 relevant articles per cycle rather than 140–200 with a 4-day window, reducing noise and LLM prompt length.

---

## Running locally

### Prerequisites
- Python 3.9+
- [Ollama](https://ollama.com) with `qwen2.5:7b` pulled
- (Optional) Cloudflare tunnel for public access

### Start

```bash
# Pull the model once
ollama pull qwen2.5:7b

# Start the proxy
cd wb_election_intel
python3 proxy_local.py --backend ollama

# Open in browser
open http://localhost:5050
```

### Alternative backends (if no local GPU)

```bash
# Groq (free tier, fast)
python3 proxy_local.py --backend groq --key gsk_YOUR_KEY

# Google Gemini (free tier)
python3 proxy_local.py --backend gemini --key AIza_YOUR_KEY
```

### Public access via Cloudflare tunnel

```bash
# One-time setup
cloudflared tunnel create wb-elections-live
cloudflared tunnel route dns wb-elections-live your-domain.com

# Run
cloudflared tunnel --config ~/.cloudflared/config.yml run wb-elections-live
```

---

## Security notes

- All credentials (`telegram_config.json`, `.env`, `*.session`) are `chmod 600` and gitignored — never committed
- The Cloudflare tunnel provides HTTPS termination; the local proxy runs HTTP on localhost only
- All `/api/*` calls in the frontend use `window.location.origin` — works correctly over both localhost and the HTTPS tunnel without hardcoded URLs
- The SQLite database is gitignored and stays local

---

## Observation context

Built for Phase 1 of the West Bengal Assembly Elections 2026 (23 April 2026). Primary coverage: **Bankura district** and its assembly constituencies — Saltora, Chhatna, Ranibandh (ST), Raipur (ST), Taldangra, Barjora, Onda, Sonamukhi, Kotulpur (SC), Indas, Bishnupur.

Extended coverage: Purulia, Jhargram, Birbhum, Paschim Bardhaman, Paschim Medinipur.

---

## Disclaimer

This tool is for election observation and public interest journalism purposes. All data is sourced from publicly available news feeds. The LLM analysis is an automated first-pass assessment and must be verified by a qualified human observer before any official action is taken. Threat levels are indicative, not definitive.

---

*Built with Python, Ollama, Chart.js, Leaflet, and Cloudflare Tunnel.*
