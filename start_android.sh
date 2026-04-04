#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  WB Election Intel — Android / Termux Launcher
#
#  SETUP (one time only):
#    1. Install "Termux" from F-Droid (not Play Store)
#    2. In Termux run:
#         pkg update && pkg install python
#    3. Copy this folder to your phone via USB or cloud
#    4. In Termux, navigate to this folder and run:
#         bash start_android.sh
#    5. Open Chrome and go to: http://localhost:5050
# ═══════════════════════════════════════════════════════════

cd "$(dirname "$0")"

echo ""
echo "  WB Election Intel — Android/Termux Launcher"
echo "  ============================================"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "  Python not found. Run in Termux:"
    echo "    pkg update && pkg install python"
    exit 1
fi

echo "  Python: $(python3 --version)"
echo "  Starting proxy on port 5050..."
echo ""
echo "  Open Chrome and go to: http://localhost:5050"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

python3 proxy.py --no-browser
