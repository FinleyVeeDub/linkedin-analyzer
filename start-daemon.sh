#!/bin/bash
# Start LinkedIn Background Server as daemon

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
LOG_FILE="$SCRIPT_DIR/background_server.log"
PID_FILE="$SCRIPT_DIR/background_server.pid"
export PORT="${PORT:-8766}"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "❌ venv not found at $VENV_PYTHON. Run ./scripts/install.sh first." >&2
    exit 1
fi

# Check if already running and healthy
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p $PID > /dev/null 2>&1; then
        echo "Background server already running (PID: $PID)"
        exit 0
    fi
    echo "Stale PID file found (PID: $PID) – cleaning up"
    rm -f "$PID_FILE"
fi

# Kill any stale daemon on the target port so we never end up with two servers.
# lsof is not guaranteed on every system; fall back to a python-based check.
if command -v lsof > /dev/null 2>&1; then
    STALE_PID=$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "$STALE_PID" ]; then
        echo "Port $PORT in use by stale PID(s): $STALE_PID – killing"
        kill $STALE_PID 2>/dev/null || true
        sleep 1
    fi
elif "$VENV_PYTHON" -c "import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',$PORT))" 2>/dev/null; then
    :
else
    echo "Port $PORT is already in use (lsof not available to identify the process)." >&2
fi

# Start in background
nohup "$VENV_PYTHON" "$SCRIPT_DIR/background_server.py" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 2

# Check if started successfully
if ps -p $(cat "$PID_FILE") > /dev/null 2>&1; then
    echo "✅ Background server started (PID: $(cat $PID_FILE))"
    echo "   Log: $LOG_FILE"
else
    echo "❌ Failed to start background server"
    cat "$LOG_FILE"
    exit 1
fi
