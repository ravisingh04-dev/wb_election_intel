#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  WB Election Intel — Mac Double-Click Launcher
#  To use: right-click → Open (first time to bypass Gatekeeper)
#  After that: double-click to launch
# ═══════════════════════════════════════════════════════════

# Change to the directory where this script lives
cd "$(dirname "$0")"

# Keep Terminal window open on error
trap 'echo ""; echo "  An error occurred. Press Enter to close..."; read' ERR

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║  WB Election Intel — Bankura 2026 Launcher  ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# Find Python 3
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

# Try Homebrew locations if not found in PATH
if [ -z "$PYTHON" ]; then
    for path in /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
        if [ -x "$path" ]; then
            PYTHON="$path"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "  ERROR: Python 3 not found."
    echo ""
    echo "  Option 1 — Install from python.org:"
    echo "    https://www.python.org/downloads/macos/"
    echo ""
    echo "  Option 2 — Install via Homebrew:"
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "    brew install python3"
    echo ""
    read -p "  Press Enter to close..."
    exit 1
fi

echo "  Found: $($PYTHON --version)"
echo ""
echo "  Starting proxy server..."
echo "  Your browser will open automatically."
echo ""
echo "  To stop: press Ctrl+C or close this window"
echo ""

"$PYTHON" proxy.py
