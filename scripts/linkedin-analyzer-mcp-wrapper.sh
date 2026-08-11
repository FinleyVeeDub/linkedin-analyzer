#!/usr/bin/env bash
# LinkedIn Analyzer MCP wrapper for LM Studio.
#
# OUT OF THE BOX on any machine (only Python 3.10+ and a POSIX shell assumed):
#   - locates a usable Python 3.10+ interpreter
#   - creates the virtualenv (fast)
#   - on the FIRST run the slow parts (pip deps + Playwright browser download)
#     are launched in a detached bootstrap worker so LM Studio's MCP startup
#     timeout is never hit
#   - ensures a session-encryption key exists
#   - starts the background server once its dependencies are ready
#   - finally execs the stdio MCP server
#
# All paths are derived from this script's own location; the wrapper can be
# launched from anywhere (LM Studio runs it with an arbitrary cwd).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
MCP_SCRIPT="$PROJECT_DIR/mcp-server/linkedin_mcp.py"
BG_SCRIPT="$PROJECT_DIR/background_server.py"
START_SCRIPT="$PROJECT_DIR/start-daemon.sh"
BOOTSTRAP_SCRIPT="$PROJECT_DIR/scripts/bootstrap.sh"
REQUIREMENTS_FILE="$PROJECT_DIR/requirements.txt"
LOCK_DIR="$PROJECT_DIR/.bootstrap.lock"

say() { echo "[linkedin-analyzer] $*" >&2; }

# ---------------------------------------------------------------------------
# .env support (best effort). Honor PORT/HOST etc. from the project .env so
# the MCP server, the wrapper and the daemon agree on the port.
# ---------------------------------------------------------------------------
load_dotenv() {
  local env_file="$PROJECT_DIR/.env"
  [[ -f "$env_file" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ -z "$line" ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!key:-}" ]] && continue  # never override real environment
    export "$key=$value"
  done < <(printf '%s\n' "$(cat "$env_file")")
}
load_dotenv

PORT="${PORT:-8766}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${PORT}/health}"
WAIT_SECONDS="${LINKEDIN_BG_WAIT_SECONDS:-45}"
FALLBACK_LOG="${LINKEDIN_BG_LOG_FILE:-$PROJECT_DIR/background_server.log}"
FALLBACK_PID="${LINKEDIN_BG_PID_FILE:-$PROJECT_DIR/background_server.pid}"
SESSION_KEY_FILE="${LINKEDIN_SESSION_KEY_FILE:-$HOME/.linkedin-analyzer/session.key}"

# ---------------------------------------------------------------------------
# Helpers that do not assume curl / lsof / gdate are installed.
# ---------------------------------------------------------------------------

version_ge() { # version_ge "3.10" 3 10
  local ver="$1" want_major="$2" want_minor="$3"
  local major minor
  major="${ver%%.*}"
  minor="${ver#*.}"
  minor="${minor%%.*}"
  if (( major > want_major )); then return 0; fi
  if (( major == want_major && minor >= want_minor )); then return 0; fi
  return 1
}

find_python() {
  local bin="${LINKEDIN_PYTHON:-}"
  if [[ -n "$bin" && -x "$bin" ]]; then
    PYTHON_BIN="$bin"
    return 0
  fi
  local cand ver
  for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      bin="$(command -v "$cand")"
      ver="$("$bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
      if [[ -n "$ver" ]] && version_ge "$ver" 3 10; then
        PYTHON_BIN="$bin"
        return 0
      fi
    fi
  done
  say "ERROR: No Python 3.10+ interpreter found."
  say "  Install Python (https://www.python.org/downloads/), or set LINKEDIN_PYTHON to its path."
  return 1
}

check_health() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1
    return $?
  fi
  "$VENV_PYTHON" - "$HEALTH_URL" <<'PY' >/dev/null 2>&1
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=3).read()
except Exception:
    sys.exit(1)
PY
  return $?
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

deps_ready() {
  "$VENV_PYTHON" -c "import fastapi, uvicorn, playwright, httpx, pydantic_settings, cryptography" >/dev/null 2>&1
}

browser_ready() {
  "$VENV_PYTHON" -c "import os; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ok=os.path.exists(p.chromium.executable_path); p.stop(); sys.exit(0 if ok else 1)" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Bootstrap: venv + deps + browser.
# ---------------------------------------------------------------------------

bootstrap_venv() {
  if [[ ! -x "$VENV_PYTHON" ]]; then
    say "Creating virtualenv ($PYTHON_BIN) ..."
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
      say "ERROR: could not create the virtualenv. On Debian/Ubuntu the venv module"
      say "  ships separately:  sudo apt-get install -y python3-venv"
      exit 1
    fi
  fi
}

# Install dependencies + browser in a detached worker so LM Studio's MCP
# startup timeout is not hit on the very first run. Idempotent + locked.
launch_bootstrap_worker() {
  if [[ -d "$LOCK_DIR" ]]; then
    return 0  # another process is already bootstrapping
  fi
  mkdir -p "$LOCK_DIR" 2>/dev/null || return 0
  if [[ ! -x "$BOOTSTRAP_SCRIPT" ]]; then
    say "WARNING: $BOOTSTRAP_SCRIPT missing; running bootstrap synchronously."
    rmdir "$LOCK_DIR" 2>/dev/null || true
    run_bootstrap_sync
    return 0
  fi
  nohup "$BOOTSTRAP_SCRIPT" "$PROJECT_DIR" >>"$PROJECT_DIR/boot.log" 2>&1 &
  say "First run: installing dependencies in the background (see boot.log)."
  say "  The MCP server is already up; first tool call may say 'not ready yet'."
}

# ---------------------------------------------------------------------------
# Background server.
# ---------------------------------------------------------------------------

start_background_direct() {
  if [[ -f "$FALLBACK_PID" ]]; then
    local old_pid
    old_pid="$(cat "$FALLBACK_PID" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && ps -p "$old_pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  PORT="${PORT}" nohup "$VENV_PYTHON" "$BG_SCRIPT" >"$FALLBACK_LOG" 2>&1 &
  echo $! >"$FALLBACK_PID" 2>/dev/null || true
}

print_port_hint() {
  if command -v lsof >/dev/null 2>&1; then
    local holder
    holder="$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -n 1 || true)"
    if [[ -n "$holder" ]]; then
      say "Port ${PORT} in use: $holder"
    fi
  else
    say "Port ${PORT} is occupied (lsof not available to show the process)."
  fi
}

ensure_background_server() {
  if check_health; then
    return 0
  fi

  export BROWSER_HEADLESS="${BROWSER_HEADLESS:-false}"

  if [[ -x "$START_SCRIPT" ]]; then
    "$START_SCRIPT" >/dev/null 2>&1 || true
    if wait_for_health; then
      return 0
    fi
  fi

  start_background_direct
  if wait_for_health; then
    return 0
  fi

  say "ERROR: Background server did not become healthy at $HEALTH_URL"
  print_port_hint
  say "Log: $FALLBACK_LOG"
  return 1
}

# ---------------------------------------------------------------------------
# Session encryption key.
# ---------------------------------------------------------------------------

ensure_session_secret() {
  local key_dir key_val
  key_dir="$(dirname "$SESSION_KEY_FILE")"
  mkdir -p "$key_dir"
  chmod 700 "$key_dir" 2>/dev/null || true

  if [[ ! -s "$SESSION_KEY_FILE" ]]; then
    umask 177
    "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))' >"$SESSION_KEY_FILE"
  fi

  chmod 600 "$SESSION_KEY_FILE" 2>/dev/null || true
  key_val="$(tr -d '\r\n' <"$SESSION_KEY_FILE")"

  if [[ -z "$key_val" ]]; then
    say "ERROR: Empty session key file: $SESSION_KEY_FILE"
    exit 1
  fi

  export SESSION_ENCRYPTION_KEY="$key_val"
  export SESSION_ENCRYPTION_KEY_FILE="$SESSION_KEY_FILE"
}

# ---------------------------------------------------------------------------
# Synchronous full bootstrap (used by --ensure-only and the installer).
# ---------------------------------------------------------------------------

run_bootstrap_sync() {
  bootstrap_venv
  if ! deps_ready; then
    say "Installing Python dependencies ..."
    "$VENV_PYTHON" -m pip install --quiet --upgrade pip
    "$VENV_PYTHON" -m pip install --quiet -r "$REQUIREMENTS_FILE"
  fi
  if ! browser_ready; then
    say "Downloading the Playwright browser (may take a few minutes) ..."
    "$VENV_PYTHON" -m playwright install chromium
  fi
  ensure_session_secret
  ensure_background_server
}

# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

find_python
bootstrap_venv
ensure_session_secret

if [[ "${1:-}" == "--ensure-only" ]]; then
  run_bootstrap_sync
  echo "ok"
  exit 0
fi

if ! deps_ready; then
  launch_bootstrap_worker
else
  ensure_background_server || true  # keep the MCP server alive even if the daemon is down
fi

exec "$VENV_PYTHON" "$MCP_SCRIPT" --stdio
