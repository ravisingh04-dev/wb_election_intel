#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  WB Election Intel — Local LLM Launcher (Mac/Linux)
#  Runs without any API key using Ollama + local model
# ═══════════════════════════════════════════════════════════

cd "$(dirname "$0")"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║  WB Election Intel — Local LLM Launcher     ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
echo "  Backends available:"
echo "    1) Ollama   — local, FREE, offline, no API key"
echo "    2) Groq     — cloud, FREE tier, fast, needs free key"
echo "    3) Gemini   — cloud, FREE tier, has web search, needs free key"
echo ""

read -p "  Choose backend [1/2/3] (default 1): " CHOICE
CHOICE=${CHOICE:-1}

case "$CHOICE" in
  2)
    echo ""
    echo "  Groq free key: https://console.groq.com/"
    read -p "  Enter Groq API key (gsk_...): " GKEY
    python3 proxy_local.py --backend groq --key "$GKEY"
    ;;
  3)
    echo ""
    echo "  Gemini free key: https://aistudio.google.com/app/apikey"
    read -p "  Enter Gemini API key (AIza...): " GKEY
    python3 proxy_local.py --backend gemini --key "$GKEY"
    ;;
  *)
    echo ""
    echo "  Checking Ollama..."
    if ! command -v ollama &>/dev/null; then
      echo ""
      echo "  Ollama not installed. Install it first:"
      echo "    Mac:   brew install ollama"
      echo "    Linux: curl -fsSL https://ollama.com/install.sh | sh"
      echo "    Win:   https://ollama.com/download"
      echo ""
      echo "  Then pull a model (run once):"
      echo "    ollama pull qwen2.5:7b     # recommended"
      echo ""
      read -p "  Press Enter to exit..."
      exit 1
    fi

    echo "  Ollama found."
    echo ""
    echo "  Your installed models:"
    ollama list 2>/dev/null || echo "  (none found)"
    echo ""
    echo "  Select model:"
    echo "    1) qwen2.5:7b   — best JSON + Bengali-aware  [default]"
    echo "    2) mistral      — fast, reliable"
    echo "    3) deepseek-r1  — strong analysis/reasoning"
    echo "    4) Type custom model name"
    echo ""
    read -p "  Choice [1/2/3/4, default 1]: " MCHOICE
    MCHOICE=${MCHOICE:-1}

    case "$MCHOICE" in
      2) MODEL="mistral" ;;
      3) MODEL="deepseek-r1" ;;
      4)
        read -p "  Enter model name exactly as shown in ollama list: " MODEL
        MODEL=${MODEL:-qwen2.5:7b}
        ;;
      *) MODEL="qwen2.5:7b" ;;
    esac

    echo ""
    echo "  Using model: $MODEL"

    # Pull model if not present
    if ! ollama list 2>/dev/null | grep -q "${MODEL%%:*}"; then
      echo ""
      echo "  Model '$MODEL' not found locally. Pulling from Ollama..."
      ollama pull "$MODEL"
    fi

    echo ""
    echo "  Starting Ollama server (if not already running)..."
    ollama serve &>/dev/null &
    sleep 2

    python3 proxy_local.py --backend ollama --model "$MODEL"
    ;;
esac
