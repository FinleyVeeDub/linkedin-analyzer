#!/usr/bin/env bash
# LinkedIn Analyzer - uninstall the services installed by scripts/install.sh.
#
# Removes:
#   - the background server service (launchd / systemd / nohup PID)
#   - the LM Studio mcp.json entry
#
# Keeps: venv, dependencies, browser, session key and saved sessions.
# Use --purge to also delete those.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVICE_NAME="com.linkedin-analyzer.daemon"
PID_FILE="$PROJECT_DIR/background_server.pid"

say() { echo "[linkedin-uninstall] $*" >&2; }
PURGE="${1:-}"

# ---------------------------------------------------------------------------
# 1) Stop the background daemon / service.
# ---------------------------------------------------------------------------
say "Stopping the background server ..."

stop_pidfile() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
      sleep 1
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$PID_FILE"
  fi
}

case "$(uname -s)" in
  Darwin)
    local plist="$HOME/Library/LaunchAgents/${SERVICE_NAME}.plist"
    if [[ -f "$plist" ]]; then
      launchctl unload "$plist" >/dev/null 2>&1 || true
      rm -f "$plist"
      say "  removed launchd agent $plist"
    fi
    ;;
  Linux)
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now linkedin-analyzer.service >/dev/null 2>&1 || true
      rm -f "$HOME/.config/systemd/user/linkedin-analyzer.service"
      systemctl --user daemon-reload >/dev/null 2>&1 || true
      say "  removed systemd user unit"
    fi
    ;;
esac
stop_pidfile

# ---------------------------------------------------------------------------
# 2) Remove the LM Studio MCP entry.
# ---------------------------------------------------------------------------
say "Removing the LM Studio MCP entry ..."
remove_lmstudio() {
  local mcp_file="${LMSTUDIO_MCP_FILE:-$HOME/.lmstudio/mcp.json}"
  [[ -f "$mcp_file" ]] || return 0
  "$VENV_PYTHON" - "$mcp_file" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (ValueError, OSError):
    data = {}
if not isinstance(data, dict):
    data = {}
servers = data.get("mcpServers")
if isinstance(servers, dict) and "linkedin-analyzer" in servers:
    del servers["linkedin-analyzer"]
    if not servers:
        data.pop("mcpServers", None)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
PY
  say "  removed 'linkedin-analyzer' from $mcp_file"
}
remove_lmstudio

# ---------------------------------------------------------------------------
# 3) Optional purge.
# ---------------------------------------------------------------------------
if [[ "$PURGE" == "--purge" ]]; then
  say "Purging environment and session data ..."
  rm -rf "$PROJECT_DIR/venv"
  rm -f "$PROJECT_DIR/.bootstrap.lock" "$PROJECT_DIR/.bootstrap.done"
  rm -rf "$PROJECT_DIR/browser_sessions"
  rm -rf "$HOME/.linkedin-analyzer"
  say "  removed venv, browser sessions and the session key."
fi

say "Done."
