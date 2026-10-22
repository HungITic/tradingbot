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
    open -a "MetaTrader 5"
    sleep 4
fi

export PYTHONPATH="$PROJECT_DIR"
export PYTHONUNBUFFERED=1

if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
elif [ -f "$PROJECT_DIR/venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
elif [ -n "$VIRTUAL_ENV" ] && [ -f "$VIRTUAL_ENV/bin/python" ]; then
    PYTHON_BIN="$VIRTUAL_ENV/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "[ERROR] Python interpreter not found" >&2
    exit 1
fi

# caffeinate options:
# -i: prevent system idle sleep
# -m: prevent disk idle sleep
# -s: prevent system sleep on AC power
if [ -x "/usr/bin/caffeinate" ]; then
    exec /usr/bin/caffeinate -ims "$PYTHON_BIN" "$PROJECT_DIR/scripts/run_logged.py" "$PROJECT_DIR/run.py"
else
    exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/run_logged.py" "$PROJECT_DIR/run.py"
fi
