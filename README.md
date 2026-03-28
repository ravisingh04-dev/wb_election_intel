# WB Election Intel — Bankura 2026
## Election Observer Intelligence Dashboard

A self-contained, offline-capable OSINT and intelligence dashboard for election
observers in Bankura district, West Bengal. Phase 1: **23 April 2026**.
Results: **4 May 2026**.

---

## What's in this folder

```
wb_election_intel/
│
├── wb_live_intel_dashboard.html   ← Main intelligence dashboard (Claude-powered)
├── wb_osint_monitor.html          ← OSINT source directory (manual monitoring)
│
├── proxy.py                       ← Local server (needed for browser API calls)
│
├── start_mac.command              ← Mac launcher — double-click in Finder
├── start.sh                       ← Mac/Linux launcher — run in Terminal
├── start.bat                      ← Windows launcher — double-click
├── start_android.sh               ← Android/Termux launcher
│
├── README.md                      ← This file
└── .api_key                       ← Your API key (auto-created, keep private)
```

---

## Quick start by platform

### Mac (easiest)
1. Right-click `start_mac.command` → **Open** (first time only, to bypass Gatekeeper)
2. After that, just **double-click** it any time
3. Your browser opens automatically at `http://localhost:5050`
4. Paste your API key when prompted (saved for future runs)

### Windows
1. **Double-click** `start.bat`
2. Paste your API key when prompted
3. Browser opens at `http://localhost:5050`

### Mac / Linux (Terminal)
```bash
cd wb_election_intel
bash start.sh
# or with your key directly:
bash start.sh sk-ant-api03-YOUR_KEY_HERE
```

### Android (Termux)
1. Install **Termux** from [F-Droid](https://f-droid.org/packages/com.termux/) (not Play Store)
2. In Termux:
```bash
pkg update && pkg install python
```
3. Copy this folder to your phone (USB, Google Drive, etc.)
4. In Termux, navigate to the folder:
```bash
cd /sdcard/wb_election_intel
bash start_android.sh
```
5. Open Chrome → `http://localhost:5050`

### iPhone / iPad
iOS does not allow running local servers. Options:
- Use a laptop/Mac on the same network and open `http://[laptop-ip]:5050` in Safari
- Or use the dashboard directly inside [Claude.ai](https://claude.ai) (no proxy needed there)

---

## Getting your API key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Sign in or create a free account
3. Click **API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-api03-...`)
5. Paste it when the proxy prompts, or in the dashboard sidebar

The key is saved in `.api_key` in this folder after the first run.
You won't need to paste it again on the same device.

**Keep your API key private.** Do not share this folder with the `.api_key` file.

---

## How to use the dashboard

### Normal monitoring (pre-election)
1. Start the proxy (see above)
2. Open `http://localhost:5050`
3. Click **Fetch & Analyse Now** for the first cycle
4. Dashboard auto-refreshes every **30 minutes**

### Election day (23 April 2026)
1. Click **Switch to Election Day Mode** in the sidebar
2. Auto-refresh drops to **15 minutes**
3. Each cycle searches for: Bankura incidents, MCC violations, TMC/BJP clashes,
   EVM issues, booth capture reports, paramilitary updates, disinformation

### Reading the dashboard
- **Threat level** (top right metric): LOW → MODERATE → HIGH → CRITICAL
- **Red feed items** = high severity, requires immediate attention
- **Amber items** = medium severity, monitor closely
- **Intelligence Summary** = Claude's synthesised analysis with observer actions

---

## Memory and timeline

Every fetch cycle is saved automatically in your browser's localStorage.
Data persists across restarts.

### View timeline
- Click **📜 View election timeline** in the sidebar
- Shows every cycle from newest to oldest
- Colour-coded dots: green=LOW, amber=MODERATE/HIGH, red=CRITICAL

### Export your data
From the timeline modal:
- **Export JSON** — full structured data, all cycles
- **Export CSV** — spreadsheet-friendly, one row per cycle

### Generate video
- Click **🎬 Export as video** in the sidebar
- Downloads a self-contained HTML slideshow
- One slide per intelligence cycle, auto-advancing
- To convert to video file:
  - **Mac**: QuickTime Player → File → New Screen Recording
  - **Any platform**: [OBS Studio](https://obsproject.com) (free) → Window Capture

### Transfer memory to another device
1. On device A: Timeline → **Export JSON**
2. Keep the JSON file safe — it's your full election record
3. The JSON contains timestamps, threat levels, all feed items, and analysis

---

## The OSINT source directory

Open `http://localhost:5050/osint` for the manual OSINT monitor.

This is a curated, clickable directory of:
- Bengali news channels (Zee 24 Ghanta, ABP Ananda, TV9 Bangla live streams)
- National English press (NDTV, Indian Express, The Hindu — WB filtered)
- Google News pre-filtered for each district (Bankura, Bishnupur, Purulia,
  Jhargram, Birbhum, Paschim Bardhaman)
- Social media (X/Twitter live searches, Reddit, Bengali hashtags)
- Official portals (ECI, CEO West Bengal, Bankura District, MCC guidelines)
- Fact-check / disinfo (BOOM Live, AltNews, AFP Fact Check)
- Law & order (WB Police, Bankura violence feeds)
- OSINT tools (Google Trends WB, Google Alerts, OSINT Framework)

Use this alongside the auto dashboard for manual source verification.

---

## Troubleshooting

### "Type error" or fetch fails
- Make sure the proxy is running (Terminal/Command Prompt window must stay open)
- Check the proxy shows `✓ localhost proxy active` in the dashboard sidebar
- Verify your API key at [console.anthropic.com](https://console.anthropic.com/)

### Browser won't open automatically
- Go to `http://localhost:5050` manually

### Port 5050 already in use
- The proxy automatically tries nearby ports (5051, 5052...)
- Or specify a different port: `python3 proxy.py --port 8080`

### Dashboard shows red "⚠ file:// — run proxy.py"
- You opened the HTML file directly. Always use the proxy instead.
- Start the proxy and open `http://localhost:5050`

### API key errors (401)
- Key may have expired or been revoked
- Generate a new key at [console.anthropic.com](https://console.anthropic.com/)
- Delete `.api_key` file and restart the proxy to re-enter

### Slow or no results
- Each cycle makes multiple web searches — can take 30–60 seconds
- Requires internet connection (searches live news)

---

## Network requirements

The proxy needs outbound HTTPS access to:
- `api.anthropic.com` — for Claude API calls (search + analysis)
- `fonts.googleapis.com` — for dashboard fonts (optional, works without)

No inbound connections. No data stored remotely. All intelligence data
stays in your browser's localStorage on your device.

---

## Election dates — quick reference

| Event | Date |
|---|---|
| Schedule announced | 15 March 2026 |
| MCC in force | 15 March 2026 |
| Phase 1 polling (Bankura + surrounds) | **23 April 2026** |
| Phase 2 polling (southern/eastern WB) | 29 April 2026 |
| Counting & results | **4 May 2026** |

### Bankura's 12 Assembly Constituencies

| # | Constituency | Type |
|---|---|---|
| 247 | Saltora | SC |
| 248 | Chhatna | General |
| 249 | Ranibandh | ST |
| 250 | Raipur | ST |
| 251 | Taldangra | General |
| 252 | Bankura (HQ) | General |
| 253 | Barjora | General |
| 254 | Onda | General |
| 255 | Bishnupur | General |
| 256 | Kotulpur | SC |
| 257 | Indas | SC |
| 258 | Sonamukhi | SC |

Surrounding Phase 1 districts: **Purulia, Jhargram, Birbhum,
Paschim Bardhaman, Paschim Medinipur**

---

*Built for official election observer use · All data sourced live from public web*
*No data transmitted to third parties beyond Anthropic API calls*
