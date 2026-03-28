# Local LLM Setup Guide
## Run the dashboard without Anthropic API credits

Three free alternatives to the Anthropic API, in order of recommendation:

---

## Option 1 — Ollama (Best: fully local, no internet, no API key)

Runs a language model entirely on your laptop.
**No API key. No cost. Works offline once model is downloaded.**

### Requirements
- **RAM**: 8GB minimum, 16GB recommended
- **Disk**: ~2–4GB per model
- **OS**: Mac, Windows, Linux

### Setup (one time)

**Mac:**
```bash
brew install ollama
ollama pull qwen2.5:7b   # recommended
```

**Linux:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b   # recommended
```

**Windows:**
Download from https://ollama.com/download/windows, install, then in Command Prompt:
```
ollama pull qwen2.5:7b   # recommended
```

### Start dashboard
```bash
# Mac/Linux:
bash start_local_llm.sh
# choose option 1

# Windows:
start_local_llm.bat
# choose option 1
```

Or directly:
```bash
python3 proxy_local.py --backend ollama --model qwen2.5:7b
```

### Recommended models (pick one)

| Model | Size | Quality | Best for |
|---|---|---|---|
| `qwen2.5:7b` ★ | 4.7GB | Excellent | **JSON output, Bengali text, default choice** |
| `mistral` | 4.1GB | Good | Fast general analysis |
| `deepseek-r1:7b` | 4.7GB | Excellent | Deep reasoning & assessment |
| `deepseek-r1:14b` | 9GB | Outstanding | Best analysis, needs 16GB RAM |
| `qwen2.5:14b` | 9GB | Outstanding | Best JSON+multilingual, needs 16GB RAM |

★ Recommended default — already installed, best balance of JSON reliability,
  speed, and awareness of South Asian languages including Bengali.

Pull any of these with: `ollama pull MODEL_NAME`

### How it works without web search
The local proxy fetches **live RSS feeds from Google News** for all 8 monitored
topics (Bankura election, WB violence, MCC violations, TMC/BJP clashes, etc.)
and injects the headlines directly into the prompt before sending to Ollama.
The model then analyses and formats the real news into the dashboard JSON.

---

## Option 2 — Groq (Cloud, free tier, very fast)

Groq offers a **free tier** with generous limits:
- 14,400 requests/day
- 30 requests/minute
- Models: Llama 3.3 70B, Mixtral, Gemma

### Setup
1. Go to https://console.groq.com/
2. Sign up (free, no credit card)
3. Click **API Keys** → **Create API Key**
4. Copy key (starts with `gsk_`)

### Start dashboard
```bash
python3 proxy_local.py --backend groq --key gsk_YOUR_KEY_HERE
```

Or use the launcher:
```bash
bash start_local_llm.sh   # choose option 2
```

---

## Option 3 — Google Gemini (Cloud, free tier, has web search)

Gemini 1.5 Flash is free with:
- 15 requests/minute
- 1 million tokens/day
- **Bonus**: Gemini can use Google Search (like Claude's web search)

### Setup
1. Go to https://aistudio.google.com/app/apikey
2. Sign in with Google account
3. Click **Create API Key**
4. Copy key (starts with `AIza`)

### Start dashboard
```bash
python3 proxy_local.py --backend gemini --key AIza_YOUR_KEY_HERE
```

---

## Comparison

| | Ollama | Groq | Gemini | Anthropic |
|---|---|---|---|---|
| Cost | Free | Free tier | Free tier | Paid |
| Internet needed | No* | Yes | Yes | Yes |
| Web search | Via RSS | Via RSS | Native | Native |
| Speed | Slow-medium | Very fast | Fast | Fast |
| Quality | Good | Excellent | Excellent | Excellent |
| Privacy | Full | Cloud | Cloud | Cloud |
| API key | None | Free | Free | Paid |

*Ollama needs internet only for the RSS news fetch. The LLM itself runs offline.

---

## Switching between backends

You can switch any time — just stop the proxy (Ctrl+C) and restart with a
different backend. All your stored memory (localStorage) is in the browser and
is unaffected by which backend you use.

---


## Tips for your installed models

### qwen2.5:7b (recommended default)
- Best JSON output of the three — fewest parse errors
- Multilingual: understands Bengali place names and context naturally
- Start with this one

### mistral
- Fastest response times
- Good general news analysis
- Occasionally adds extra text around JSON — the `repairJSON()` function handles this

### deepseek-r1 (deepseek-r1:7b or similar)
- Strongest reasoning and analytical depth
- May produce longer, more thoughtful assessments
- Slightly slower — worth it for the analysis quality
- Uses a thinking/reasoning step before answering — normal behaviour

### Switching models without restarting
Stop the proxy (Ctrl+C) and restart with a different `--model` flag:
```bash
python3 proxy_local.py --backend ollama --model mistral
python3 proxy_local.py --backend ollama --model deepseek-r1:7b
python3 proxy_local.py --backend ollama --model qwen2.5:7b
```

## Troubleshooting

### Ollama: "connection refused"
Ollama server may not be running. Start it:
```bash
ollama serve
```

### Ollama: slow responses
Normal for local models — allow 30–120 seconds per cycle.
Try `mistral` (fastest) or `qwen2.5:7b` (best JSON reliability).
For `deepseek-r1`, responses may be slower but more analytical.

### Groq: rate limit errors
You've hit the free tier limit (30 req/min or 14,400/day).
Wait a minute, or switch to Gemini.

### JSON parse errors with local models
Local models sometimes add extra text around the JSON.
The dashboard's `repairJSON()` function handles most cases.
If it fails repeatedly, try a larger/better model.

### No news fetched
Check your internet connection. The RSS fetcher needs internet access
to pull Google News feeds. The LLM itself doesn't need internet (for Ollama).
