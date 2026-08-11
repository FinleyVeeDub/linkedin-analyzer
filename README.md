# LinkedIn Analyzer

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

- Python 3.10+
- [LM Studio](https://lmstudio.ai/) (local)
- A LinkedIn account (you will log in manually once)

## Installation

```bash
# 1. Clone and enter the repo
git clone <your-repo-url>
cd linkedin-analyzer

# 2. Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate      # macOS/Linux
# or: venv\Scripts\activate    # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install the Playwright browser
playwright install chromium

# 5. Configure
cp .env.example .env
# edit .env as needed (see below)
```

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

### Option B — Auto-start wrapper (recommended for LM Studio)

Use `scripts/linkedin-mcp-wrapper.sh` as the MCP command in LM Studio. It
ensures a session key exists, starts the background server if needed, waits
until it is healthy, then runs the MCP server:

```json
{
  "mcpServers": {
    "linkedin-analyzer": {
      "command": "/path/to/linkedin-analyzer/scripts/linkedin-mcp-wrapper.sh",
      "args": ["--stdio"]
    }
  }
}
```

## Setup in LM Studio

1. Add the MCP server (see above) and restart LM Studio.
2. Start a chat with the system prompt — pick `docs/system-prompt.en.md` (or the German `docs/system-prompt.de.md`) and paste it in.
3. Ask the assistant to check your session or analyze your profile.

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

**Browser does not open**
- Make sure `BROWSER_HEADLESS=False` in `.env` (or unset).
- Reinstall the browser: `playwright install chromium`

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
│   └── linkedin-mcp-wrapper.sh # Optional auto-start wrapper for LM Studio
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
