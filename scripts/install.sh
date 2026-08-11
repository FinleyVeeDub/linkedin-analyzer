#!/usr/bin/env bash
# LinkedIn Analyzer - install all required services so the MCP server runs in
# LM Studio on this machine. Works without any pre-installed services other
# than Python 3.10+ and a POSIX shell.
#
# What it does:
#   1. Builds the environment (venv, Python deps, Playwright browser, session key)
#   2. Installs the background server as a real service so it survives reboots:
#        - macOS:  a launchd LaunchAgent
#        - Linux:  a systemd --user unit (fallback: nohup daemon)
#        - other:  nohup daemon
#   3. Registers the MCP server in LM Studio's mcp.json (absolute path, no
#      manual editing)
#   4. Verifies health
#
# Usage: ./scripts/install.sh
#        ./scripts/uninstall.sh            # removes service + LM Studio entry
#        ./scripts/uninstall.sh --purge    # additionally removes venv + sessions
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$PROJECT_DIR/scripts/linkedin-analyzer-mcp-wrapper.sh"
BG_SCRIPT="$PROJECT_DIR/background_server.py"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVICE_NAME="com.linkedin-analyzer.daemon"
LOG_FILE="$PROJECT_DIR/background_server.log"
PID_FILE="$PROJECT_DIR/background_server.pid"

say() { echo "[linkedin-install] $*" >&2; }
die() { say "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# 1) Environment bootstrap (venv, deps, browser, session key, daemon up).
# ---------------------------------------------------------------------------
say "Step 1/4: building the environment (venv, deps, Playwright browser) ..."
"$WRAPPER" --ensure-only >/dev/null
say "Step 1/4: environment ready."

PORT_FROM_ENV="$(grep -E '^PORT=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2 || true)"
PORT="${PORT_FROM_ENV:-8766}"
export PORT

# ---------------------------------------------------------------------------
# 2) Background service.
# ---------------------------------------------------------------------------
say "Step 2/4: installing the background server as a service ..."

install_launchd() {
  local plist="$HOME/Library/LaunchAgents/${SERVICE_NAME}.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${SERVICE_NAME}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV_PYTHON}</string>
    <string>${BG_SCRIPT}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PORT</key><string>${PORT}</string>
    <key>BROWSER_HEADLESS</key><string>false</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG_FILE}</string>
  <key>StandardErrorPath</key><string>${LOG_FILE}</string>
</dict>
</plist>
PLIST
  # Unload a previous instance first (ignore failures), then load.
  launchctl unload "$plist" >/dev/null 2>&1 || true
  launchctl load -w "$plist"
  say "  launchd agent installed: $plist"
}

install_systemd() {
  local unit_dir="$HOME/.config/systemd/user"
  local unit="$unit_dir/linkedin-analyzer.service"
  mkdir -p "$unit_dir"
  cat >"$unit" <<UNIT
[Unit]
Description=LinkedIn Analyzer background server
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_PYTHON} ${BG_SCRIPT}
Environment=PORT=${PORT}
Environment=BROWSER_HEADLESS=false
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now linkedin-analyzer.service
  say "  systemd user unit installed: $unit"
  say "  NOTE: run 'loginctl enable-linger \$(whoami)' if you want it to start"
  say "  automatically when you are not logged in."
}

start_nohup_fallback() {
  nohup "$VENV_PYTHON" "$BG_SCRIPT" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  say "  daemon started with nohup (PID $(cat "$PID_FILE")) - install a service"
  say "  manager (launchd/systemd) to have it auto-start on boot."
}

case "$(uname -s)" in
  Darwin)   install_launchd ;;
  Linux)
    if command -v systemctl >/dev/null 2>&1; then install_systemd; else start_nohup_fallback; fi
    ;;
  *)        start_nohup_fallback ;;
esac
say "Step 2/4: service installed."

# ---------------------------------------------------------------------------
# 3) Register MCP server in LM Studio (Cursor-style mcp.json).
# ---------------------------------------------------------------------------
say "Step 3/4: registering the MCP server in LM Studio ..."

register_lmstudio() {
  local mcp_file="${LMSTUDIO_MCP_FILE:-$HOME/.lmstudio/mcp.json}"
  "$VENV_PYTHON" - "$mcp_file" "$WRAPPER" <<'PY'
import json, os, sys

path, command = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        data = {}

servers = data.setdefault("mcpServers", {})
servers["linkedin-analyzer"] = {
    "command": command,
    "env": {
        "BROWSER_HEADLESS": os.environ.get("BROWSER_HEADLESS", "false"),
    },
}

os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print(path)
PY
  say "  MCP server registered at $mcp_file"
  say "  Restart LM Studio (or re-open Developer > MCP) to pick it up."
}
register_lmstudio
say "Step 3/4: LM Studio registration complete."

# ---------------------------------------------------------------------------
# 4) Verify.
# ---------------------------------------------------------------------------
say "Step 4/4: verifying ..."
if "$VENV_PYTHON" -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=5).read().decode())" >/dev/null 2>&1; then
  say "  Background server healthy at http://127.0.0.1:${PORT}"
else
  say "  WARNING: background server not healthy yet; check $LOG_FILE"
fi
if [[ -x "$VENV_PYTHON" ]]; then
  say "  venv OK: $VENV_PYTHON"
fi

echo ""
say "Done. To use it in LM Studio:"
say "  1. Restart LM Studio."
say "  2. Program tab (right sidebar) -> the 'linkedin-analyzer' MCP server should appear."
say "  3. Paste docs/system-prompt.de.md (or .en.md) as the system prompt and chat."
echo "ok"
