#!/bin/bash
# Stop LinkedIn Background Server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/background_server.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found. Server may not be running."
    exit 0
fi

PID=$(cat "$PID_FILE")

if ps -p $PID > /dev/null 2>&1; then
    kill $PID
    sleep 1
    if ps -p $PID > /dev/null 2>&1; then
        kill -9 $PID
    fi
    echo "✅ Background server stopped (PID: $PID)"
else
    echo "Server not running (stale PID file)"
fi

rm -f "$PID_FILE"
