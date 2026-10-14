#!/usr/bin/env bash
# ==============================================================================
# Trading Bot Daemon Runner
# Prevents macOS sleep (caffeinate) and launches Trading Bot
# ==============================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# Ensure logs directory exists
mkdir -p "$PROJECT_DIR/logs"

# Auto-launch MetaTrader 5 if not currently running
if [ -d "/Applications/MetaTrader 5.app" ] && ! pgrep -f "MetaTrader 5" >/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] MetaTrader 5 is not running. Auto-launching..."
    open -a "MetaTrader 5"
    sleep 4
fi

export PYTHONPATH="$PROJECT_DIR"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

if [ ! -f "$PYTHON_BIN" ]; then
    echo "[ERROR] Virtualenv python not found at $PYTHON_BIN" >&2
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Trading Bot with sleep prevention..."

# caffeinate options:
# -i: prevent system idle sleep
# -m: prevent disk idle sleep
# -s: prevent system sleep on AC power
if [ -x "/usr/bin/caffeinate" ]; then
    exec /usr/bin/caffeinate -ims "$PYTHON_BIN" "$PROJECT_DIR/run.py"
else
    exec "$PYTHON_BIN" "$PROJECT_DIR/run.py"
fi
