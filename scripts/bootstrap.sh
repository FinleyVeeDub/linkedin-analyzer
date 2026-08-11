#!/usr/bin/env bash
# LinkedIn Analyzer - detached first-run installer.
#
# Launched (nohup) by linkedin-analyzer-mcp-wrapper.sh so a slow first run
# (pip install + Playwright browser download) never blocks LM Studio's MCP
# startup. Idempotent and lock-guarded: only one bootstrap runs at a time.
#
# Usage: scripts/bootstrap.sh [PROJECT_DIR]
set -euo pipefail

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$PROJECT_DIR"

VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
BG_SCRIPT="$PROJECT_DIR/background_server.py"
START_SCRIPT="$PROJECT_DIR/start-daemon.sh"
REQUIREMENTS_FILE="$PROJECT_DIR/requirements.txt"
LOCK_DIR="$PROJECT_DIR/.bootstrap.lock"
DONE_MARKER="$PROJECT_DIR/.bootstrap.done"

say() { echo "[linkedin-bootstrap] $*" >&2; }

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
    [[ -n "${!key:-}" ]] && continue
    export "$key=$value"
  done < <(printf '%s\n' "$(cat "$env_file")")
}
load_dotenv

PORT="${PORT:-8766}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

if [[ -d "$LOCK_DIR" ]]; then
  say "Another bootstrap is already running; exiting."
  exit 0
fi
mkdir -p "$LOCK_DIR"
trap 'rm -rf "$LOCK_DIR"' EXIT

find_python() {
  local cand ver bin
  for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      bin="$(command -v "$cand")"
      ver="$("$bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
      if [[ -n "$ver" ]]; then
        PYTHON_BIN="$bin"
        return 0
      fi
    fi
  done
  say "ERROR: no Python found. Install Python 3.10+."
  exit 1
}

if [[ ! -x "$VENV_PYTHON" ]]; then
  find_python
  say "Creating virtualenv ..."
  "$PYTHON_BIN" -m venv "$PROJECT_DIR/venv"
fi

if ! "$VENV_PYTHON" -c "import fastapi, uvicorn, playwright, httpx, pydantic_settings, cryptography" >/dev/null 2>&1; then
  say "Installing Python dependencies ..."
  "$VENV_PYTHON" -m pip install --quiet --upgrade pip
  "$VENV_PYTHON" -m pip install --quiet -r "$REQUIREMENTS_FILE"
fi

if ! "$VENV_PYTHON" -c "import os; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ok=os.path.exists(p.chromium.executable_path); p.stop(); sys.exit(0 if ok else 1)" >/dev/null 2>&1; then
  say "Downloading the Playwright browser (may take a few minutes) ..."
  "$VENV_PYTHON" -m playwright install chromium
fi

check_health() {
  "$VENV_PYTHON" -c "import urllib.request; urllib.request.urlopen('$HEALTH_URL', timeout=3).read()" >/dev/null 2>&1
}

if ! check_health; then
  say "Starting the background server ..."
  if [[ -x "$START_SCRIPT" ]]; then
    "$START_SCRIPT" >/dev/null 2>&1 || true
  fi
  if ! check_health; then
    PORT="${PORT}" nohup "$VENV_PYTHON" "$BG_SCRIPT" >>"$PROJECT_DIR/background_server.log" 2>&1 &
    echo $! >"$PROJECT_DIR/background_server.pid" 2>/dev/null || true
  fi
fi

touch "$DONE_MARKER"
say "Bootstrap finished."
