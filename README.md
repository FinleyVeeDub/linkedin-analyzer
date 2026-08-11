# LinkedIn Analyzer

[![License](https://img.shields.io/github/license/FinleyVeeDub/linkedin-analyzer)](LICENSE)
[![Release](https://img.shields.io/github/v/release/FinleyVeeDub/linkedin-analyzer)](https://github.com/FinleyVeeDub/linkedin-analyzer/releases)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)

Analyze your own LinkedIn profile through [LM Studio](https://lmstudio.ai/) using an MCP server and Playwright — **without** the official LinkedIn API.

The tool reuses your existing browser session (including 2FA) to fetch your profile data as structured JSON, which a local LLM can then analyze for optimization opportunities.

## ⚠️ Legal / Compliance notice

- This tool uses **browser automation**, not LinkedIn's official API.
- Using it may violate LinkedIn's **Terms of Service**. Account restrictions are theoretically possible.
- **Use it only for your own profile, for personal, non-commercial purposes. Do not scrape third-party profiles, do not hammer the site.**
- You are solely responsible for how you use this software.

## Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────────────┐      ┌──────────┐
│ LM Studio   │ ───→ │ MCP Server   │ ───→ │ Background Server   │ ───→ │ Browser  │
│ (LLM)       │ MCP  │ (stdio)      │ HTTP │ (127.0.0.1:8766)    │      │ Playwright│
└─────────────┘      └──────────────┘      └─────────────────────┘      └──────────┘
```

| Component | Purpose |
|-----------|---------|
| `mcp-server/linkedin_mcp.py` | MCP server (stdio JSON-RPC) exposed to LM Studio |
| `background_server.py` | Long-running daemon that keeps the browser open (port **8766**) |
| `session_manager.py` | Encrypts and persists the browser session locally |
| `config.py` | Environment-driven configuration |

## Prerequisites

The repo assumes **nothing** except:

- **Python 3.10+** (Linux/macOS/Windows via WSL — a POSIX shell)
- [LM Studio](https://lmstudio.ai/) (local, version 0.3.17+ with MCP support)
- A LinkedIn account (you will log in manually once)

Everything else is installed by the repo itself: a virtualenv, the Python
dependencies, the Playwright browser (a managed
[Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)
build — no separate Chrome install needed), a session-encryption key, the
background server as an auto-start **service** (launchd on macOS, systemd on
Linux, nohup fallback otherwise), and the MCP registration inside LM Studio.
`curl`, `lsof`, `gcc` or any other CLI tools are **not** required.

## Installation

```bash
# 1. Clone and enter the repo
git clone https://github.com/FinleyVeeDub/linkedin-analyzer.git
cd linkedin-analyzer

# 2. Configure (optional - all values have working defaults)
cp .env.example .env
# edit .env as needed (see below)

# 3. Install everything in one go: builds the environment, installs the
#    background server as an auto-start service, and registers the MCP server
#    in LM Studio (no manual JSON editing):
./scripts/install.sh
```

`install.sh` prints `ok` when done. It is safe to re-run (idempotent). To undo
everything, run `./scripts/uninstall.sh` (or `./scripts/uninstall.sh --purge`
to also remove the venv and saved sessions).

> On Debian/Ubuntu you may need the venv module first:
> `sudo apt-get install -y python3-venv` — the scripts tell you this if it is
> missing.

### What gets installed

| Service | Where | Purpose |
|---------|-------|---------|
| Python virtualenv | `venv/` | Isolated Python environment |
| Python deps + Playwright browser | `venv/` | The only external dependencies |
| Session encryption key | `~/.linkedin-analyzer/session.key` | Encrypts the saved session |
| Background server **service** | launchd agent / systemd user unit | Keeps the browser open, auto-starts on boot |
| MCP server entry | `~/.lmstudio/mcp.json` | Lets LM Studio launch the MCP server |

### First run inside LM Studio (no `install.sh`)

If you skip `install.sh`, the wrapper
(`scripts/linkedin-analyzer-mcp-wrapper.sh`) bootstraps everything itself the
first time LM Studio starts it. The slow part (downloading the Playwright
browser) runs in the **background** so it does not hit LM Studio's MCP startup
timeout; the first tool call may answer "not ready yet" for a few seconds.

### Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `127.0.0.1` | Bind address of the background server |
| `PORT` | `8766` | Port of the background server |
| `SESSION_DIR` | `./browser_sessions` | Where the encrypted session file is stored |
| `SESSION_ENCRYPTION_KEY` | *(empty)* | Optional explicit Fernet key |
| `SESSION_ENCRYPTION_KEY_FILE` | `~/.linkedin-analyzer/session.key` | Key file, created automatically if missing |
| `BROWSER_HEADLESS` | `False` | `True` for automated/headless runs |

## Running

### Option A — Start the daemon manually

```bash
./start-daemon.sh          # starts background_server.py on PORT (default 8766)
./stop-daemon.sh           # stops it again
```

The background server **must keep running** while you use the MCP tools — it holds the browser open.

Verify it is up:

```bash
curl http://127.0.0.1:8766/health
```

### Option B — Auto-start wrapper (used by LM Studio)

`scripts/linkedin-analyzer-mcp-wrapper.sh` is the command LM Studio launches.
The wrapper locates Python 3.10+ itself, builds the environment on first run
(the slow steps in the background), ensures a session key exists, starts the
background server once its dependencies are ready, and then runs the MCP server
in **stdio mode itself**:

```json
{
  "mcpServers": {
    "linkedin-analyzer": {
      "command": "/path/to/linkedin-analyzer/scripts/linkedin-analyzer-mcp-wrapper.sh"
    }
  }
}
```

> **Note:** `install.sh` writes this entry for you into `~/.lmstudio/mcp.json`
> with the correct absolute path. If you write it by hand, replace
> `/path/to/linkedin-analyzer` with the real absolute path of your clone. No
> `args` are needed — the wrapper starts the stdio MCP server itself.

## Setup in LM Studio

1. Run `./scripts/install.sh` once in the terminal (recommended).
2. Restart LM Studio, switch to the **Program** tab in the right sidebar — the
   `linkedin-analyzer` MCP server should be listed as connected.
3. Start a chat with the system prompt — pick `docs/system-prompt.en.md` (or the German `docs/system-prompt.de.md`) and paste it in.
4. Ask the assistant to check your session or analyze your profile.

If the MCP server does not show up, re-add it manually via *Install > Edit
mcp.json* with the snippet from [Option B](#option-b--auto-start-wrapper-used-by-lm-studio),
then fully restart LM Studio (a cached server definition may otherwise keep the
old command).

### First login (one-time)

1. Call the `linkedin_login` tool — the browser opens at LinkedIn's login page.
2. Log in manually in the browser (2FA is supported).
3. The session **auto-saves** as soon as the login is detected. You can also force it with `linkedin_save_session`.
4. Verify with `linkedin_check_session`.

## MCP tools

| Tool | Description |
|------|-------------|
| `linkedin_check_session` | Check whether a session exists and you are logged in |
| `linkedin_login` | Open the browser at LinkedIn's login page (does **not** wait for login) |
| `linkedin_save_session` | Persist the current session after a manual login |
| `linkedin_get_profile` | Fetch the raw profile data (JSON) |
| `linkedin_analyze_profile` | Fetch the profile data for analysis |
| `linkedin_clear_session` | Delete the saved session |

## HTTP API (background server)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/check` | GET | Session/login status |
| `/login` | POST | Open the browser at the login page |
| `/save` | POST | Persist the current session |
| `/session` | DELETE | Clear the saved session |
| `/profile` | GET | Full profile data (name, headline, about, experience, education, skills) |
| `/debug`, `/debug/dom`, `/debug/selectors` | GET | Debugging aids |

## Security

| Aspect | Implementation |
|--------|----------------|
| **Passwords** | Never stored in code |
| **Session** | Stored locally and encrypted with Fernet (`browser_sessions/`) |
| **2FA** | Fully supported during the initial login |
| **Data** | Stays on your machine; the LLM only sees what you send it |

**Important:** The `browser_sessions/` files contain login tokens. They are
encrypted, but treat them like passwords. The key lives in
`SESSION_ENCRYPTION_KEY_FILE` (default `~/.linkedin-analyzer/session.key`).
Both are git-ignored — never commit them.

## Troubleshooting

**"Not logged in" / authwall redirects**
```bash
curl -X DELETE http://127.0.0.1:8766/session   # clear session
curl -X POST http://127.0.0.1:8766/login       # log in again in the opened browser
```

**MCP server does not initialize in LM Studio**
- Run `./scripts/install.sh` once, restart LM Studio, and check the Program tab.
- After changing the command, **remove the MCP server and re-add it** in LM Studio,
  then fully restart LM Studio (a cached server definition may otherwise keep the
  old command).
- Test the MCP handshake directly in a terminal:
  `printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | ./scripts/linkedin-analyzer-mcp-wrapper.sh`
  You should get a JSON `result` back (no error).
- If the wrapper fails, it writes progress to `boot.log` in the project directory
  and the daemon logs to `background_server.log`.

**First tool call says "Background server unreachable / not ready yet"**
- On a very first run the Playwright browser is still downloading in the
  background. Wait a few seconds and call the tool again.
- If it persists: check `background_server.log`, or run
  `./scripts/linkedin-analyzer-mcp-wrapper.sh --ensure-only` once in the terminal
  (blocks until everything is ready, prints `ok`).

**Browser does not open**
- Make sure `BROWSER_HEADLESS=False` in `.env` (or unset).
- Reinstall the browser: `venv/bin/python -m playwright install chromium`
- On Linux only, you may also need system libraries:
  `venv/bin/python -m playwright install-deps`

**Server not reachable**
- Is it running? `curl http://127.0.0.1:8766/health`
- Port taken? Set another `PORT` in `.env` (the wrapper respects `PORT` too).

**LinkedIn shows a captcha / rate limit**
- Wait a few hours.
- Clear the session and log in again.
- Do not send requests too frequently.

## Project layout

```
linkedin-analyzer/
├── background_server.py        # Long-running daemon (keeps browser open, HTTP API)
├── session_manager.py          # Encrypted session persistence
├── config.py                   # Environment configuration
├── mcp-server/
│   └── linkedin_mcp.py         # MCP server for LM Studio (stdio)
├── scripts/
│   └── linkedin-analyzer-mcp-wrapper.sh # Auto-start wrapper: bootstraps env + stdio MCP
├── docs/
│   ├── system-prompt.en.md     # English system prompt for LM Studio
│   └── system-prompt.de.md     # German system prompt for LM Studio
├── start-daemon.sh / stop-daemon.sh
├── requirements.txt
├── .env.example
└── browser_sessions/           # Encrypted sessions (git-ignored)
```

## Development

Test the MCP server directly:

```bash
venv/bin/python mcp-server/linkedin_mcp.py --stdio
```

Then send JSON-RPC lines over stdin (`initialize`, `tools/list`, `tools/call`).

## License

MIT — see [LICENSE](LICENSE).

## Credits

- [Playwright](https://playwright.dev/) — browser automation
- [FastAPI](https://fastapi.tiangolo.com/) — API framework
- [MCP Protocol](https://modelcontextprotocol.io/) — LLM integration
