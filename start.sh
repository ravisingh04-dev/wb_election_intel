#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  WB Election Intel — Quick Launcher (Mac / Linux)
#  Double-click or run:  bash start.sh
# ═══════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║  WB Election Intel — Bankura 2026 Launcher  ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# Check Python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        if [ "$VER" = "3" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "  ERROR: Python 3 not found."
    echo ""
    echo "  Install it from: https://www.python.org/downloads/"
    echo "  Or on Mac with Homebrew:  brew install python3"
    echo ""
    read -p "  Press Enter to exit..."
    exit 1
fi

echo "  Python found: $($PYTHON --version)"

# Pass API key as argument if provided, else proxy will prompt or use saved key
if [ -n "$1" ]; then
    echo "  Starting proxy with provided API key..."
    "$PYTHON" proxy.py "$1"
else
    "$PYTHON" proxy.py
fi
