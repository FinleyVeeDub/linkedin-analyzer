#!/usr/bin/env bash
# LinkedIn Analyzer MCP wrapper for LM Studio.
#
# Ensures a session-encryption key exists, starts the background server if it
# is not already healthy, waits for it, then execs the MCP server (stdio).
#
# All locations are derived from the project dir (the script's own location),
# so this works from any clone. Override with env vars if you need to.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${LINKEDIN_PYTHON:-$PROJECT_DIR/venv/bin/python}"
MCP_SCRIPT="$PROJECT_DIR/mcp-server/linkedin_mcp.py"
BG_SCRIPT="$PROJECT_DIR/background_server.py"
START_SCRIPT="$PROJECT_DIR/start-daemon.sh"

PORT="${LINKEDIN_BG_PORT:-8766}"
HEALTH_URL="${LINKEDIN_BG_HEALTH_URL:-http://127.0.0.1:${PORT}/health}"
WAIT_SECONDS="${LINKEDIN_BG_WAIT_SECONDS:-25}"
FALLBACK_LOG="${LINKEDIN_BG_LOG_FILE:-/tmp/linkedin-background-server.log}"
FALLBACK_PID="${LINKEDIN_BG_PID_FILE:-/tmp/linkedin-background-server.pid}"
SESSION_KEY_FILE="${LINKEDIN_SESSION_KEY_FILE:-$HOME/.linkedin-analyzer/session.key}"

check_health() {
  curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1
}

wait_for_health() {
  local i
  for ((i = 1; i <= WAIT_SECONDS; i++)); do
    if check_health; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_background_direct() {
  if [[ -f "$FALLBACK_PID" ]]; then
    local old_pid
    old_pid="$(cat "$FALLBACK_PID" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && ps -p "$old_pid" >/dev/null 2>&1; then
      return 0
    fi
  fi

  PORT="${PORT}" nohup "$PYTHON_BIN" "$BG_SCRIPT" >"$FALLBACK_LOG" 2>&1 &
  echo $! >"$FALLBACK_PID" 2>/dev/null || true
}

print_port_hint() {
  local holder
  holder="$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -n 1 || true)"
  if [[ -n "$holder" ]]; then
    echo "[linkedin-mcp-wrapper] Port ${PORT} in use: $holder" >&2
  fi
}

ensure_session_secret() {
  local key_dir key_val
  key_dir="$(dirname "$SESSION_KEY_FILE")"
  mkdir -p "$key_dir"
  chmod 700 "$key_dir" 2>/dev/null || true

  if [[ ! -s "$SESSION_KEY_FILE" ]]; then
    umask 177
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$SESSION_KEY_FILE"
  fi

  chmod 600 "$SESSION_KEY_FILE" 2>/dev/null || true
  key_val="$(tr -d '\r\n' <"$SESSION_KEY_FILE")"

  if [[ -z "$key_val" ]]; then
    echo "[linkedin-mcp-wrapper] ERROR: Empty session key file: $SESSION_KEY_FILE" >&2
    exit 1
  fi

  export SESSION_ENCRYPTION_KEY="$key_val"
  export SESSION_ENCRYPTION_KEY_FILE="$SESSION_KEY_FILE"
}

ensure_background_server() {
  if check_health; then
    return 0
  fi

  # Force a visible browser for the login flow unless explicitly overridden.
  export BROWSER_HEADLESS="${BROWSER_HEADLESS:-false}"

  # Preferred start path from the project, if available.
  if [[ -x "$START_SCRIPT" ]]; then
    "$START_SCRIPT" >/dev/null 2>&1 || true
    if wait_for_health; then
      return 0
    fi
  fi

  # Fallback for environments where the project dir is not writable.
  start_background_direct

  if wait_for_health; then
    return 0
  fi

  echo "[linkedin-mcp-wrapper] ERROR: Background server did not become healthy at $HEALTH_URL" >&2
  print_port_hint
  echo "[linkedin-mcp-wrapper] Hint: check for a stale process on :${PORT}." >&2
  echo "[linkedin-mcp-wrapper] Fallback log: $FALLBACK_LOG" >&2
  exit 1
}

if [[ "${1:-}" == "--ensure-only" ]]; then
  ensure_session_secret
  ensure_background_server
  echo "ok"
  exit 0
fi

ensure_session_secret
ensure_background_server
exec "$PYTHON_BIN" "$MCP_SCRIPT" --stdio
