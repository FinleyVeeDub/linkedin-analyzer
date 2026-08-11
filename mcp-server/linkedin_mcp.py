#!/usr/bin/env python3
"""
LinkedIn MCP Server - stdio JSON-RPC client for the LinkedIn background server.

Runs the Model Context Protocol over stdin/stdout (newline-delimited JSON-RPC)
exactly as LM Studio expects. Pure standard library: it must start and answer
`initialize`/`tools/list`/`ping` even before any third-party dependency or the
background daemon is ready, so a first run in LM Studio never fails on imports.

Protocol notes (2024-11-05):
  * Never emit anything to stdout before an `initialize` request.
  * Never respond to a notification (a message without an `id`).
  * `ping` is answered with an empty result; `shutdown` + `notifications/exit`
    terminates the loop cleanly.
  * If the background server is unreachable, tools return a JSON error *as the
    tool result* instead of crashing the connection.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "linkedin-analyzer"
SERVER_VERSION = "2.1.0"
PROTOCOL_VERSION = "2025-11-25"
KNOWN_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}

TOOLS = [
    {
        "name": "linkedin_get_profile",
        "description": "Get LinkedIn profile data.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "linkedin_check_session",
        "description": "Check login status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "linkedin_login",
        "description": "Open browser at LinkedIn login. Browser STAYS OPEN.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "linkedin_save_session",
        "description": "Save session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "linkedin_analyze_profile",
        "description": "Get profile with analysis prompt.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "linkedin_get_posts",
        "description": "Get your own recent LinkedIn posts from the activity tab "
                     "(text, author, timestamp, reactions/comments/reposts, post URL).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max posts to return (default 10)."},
                "scroll": {"type": "integer", "description": "Lazy-load scroll passes (default 3)."},
            },
        },
    },
    {
        "name": "linkedin_get_post",
        "description": "Get one LinkedIn post by URL, e.g. "
                     "https://www.linkedin.com/feed/update/urn:li:activity:NNNNNN/",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full LinkedIn post URL."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "linkedin_analyze_posts",
        "description": "Get your recent LinkedIn posts with an analysis prompt "
                     "(which posts perform best and why).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max posts to return (default 10)."},
                "scroll": {"type": "integer", "description": "Lazy-load scroll passes (default 3)."},
            },
        },
    },
    {
        "name": "linkedin_clear_session",
        "description": "Clear session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_ENDPOINTS = {
    "linkedin_get_profile": ("GET", "/profile"),
    "linkedin_check_session": ("GET", "/check"),
    "linkedin_login": ("POST", "/login"),
    "linkedin_save_session": ("POST", "/save"),
    "linkedin_analyze_profile": ("GET", "/profile"),
    "linkedin_get_posts": ("GET", "/posts"),
    "linkedin_get_post": ("GET", "/post"),
    "linkedin_analyze_posts": ("GET", "/posts"),
    "linkedin_clear_session": ("DELETE", "/session"),
}

# Only these arguments are forwarded to the background server as query params.
# Everything else in `arguments` is ignored (belt and braces against prompt injection).
PARAM_WHITELIST = {
    "linkedin_get_posts": {"limit", "scroll"},
    "linkedin_get_post": {"url"},
    "linkedin_analyze_posts": {"limit", "scroll"},
}


def load_dotenv() -> None:
    """Best-effort .env reader (stdlib only). Never overrides real env vars."""
    try:
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
        with open(env_file, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key.isidentifier() or key in os.environ:
                    continue
                os.environ[key] = value.split("#")[0].strip().strip('"').strip("'")
    except OSError:
        pass


def background_base_url() -> str:
    host = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("PORT", "8766").strip() or "8766"
    return f"http://{host}:{port}"


def http_call(method: str, path: str, timeout: float = 60.0) -> dict:
    """Proxy a tool call to the background server. Never raises."""
    url = background_base_url() + path
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except (ValueError, TypeError):
                return {"error": f"Non-JSON response from {url}: {body[:200]}"}
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8", "replace"))
        except (ValueError, TypeError):
            return {"error": f"Background server HTTP {error.code} from {url}"}
    except Exception as error:  # noqa: BLE001 - report any transport failure to the LLM
        return {
            "error": (
                f"Background server unreachable at {url} ({error}). "
                "It is starting up, not installed yet, or stopped. "
                "Run the repo's install script once, or ./start-daemon.sh, then retry."
            )
        }


def _text_result(data: dict) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}
        ]
    }


def handle_message(message: dict):
    """Return a response dict, None (no reply), or the sentinel EXIT."""
    method = message.get("method", "")
    msg_id = message.get("id")

    # Notifications carry no id -> must never be answered.
    if msg_id is None:
        if method == "notifications/exit":
            return "EXIT"
        if method == "notifications/initialized":
            return None
        return None

    if method == "initialize":
        params = message.get("params", {}) or {}
        requested = params.get("protocolVersion", PROTOCOL_VERSION)
        chosen = requested if requested in KNOWN_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": chosen,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params", {}) or {}
        name = params.get("name", "")
        endpoint = TOOL_ENDPOINTS.get(name)
        if endpoint is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": f"Unknown tool: {name}"},
            }
        method_verb, path = endpoint
        query = ""
        whitelist = PARAM_WHITELIST.get(name, set())
        if whitelist:
            arguments = params.get("arguments", {}) or {}
            if isinstance(arguments, dict):
                filtered = {
                    key: value for key, value in arguments.items()
                    if key in whitelist and value is not None
                }
                if filtered:
                    query = "?" + urllib.parse.urlencode(filtered)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": _text_result(http_call(method_verb, path + query)),
        }

    if method in ("resources/list", "prompts/list"):
        key = "resources" if method == "resources/list" else "prompts"
        return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def run_stdio_server() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except (ValueError, TypeError) as error:
            _emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {error}"}})
            continue
        try:
            response = handle_message(message)
        except Exception as error:  # noqa: BLE001 - keep the stdio channel alive
            if message.get("id") is not None:
                _emit({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": str(error)}})
            continue
        if response == "EXIT":
            break
        if response is not None:
            _emit(response)


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "--stdio":
        run_stdio_server()
        return
    print("Usage: python linkedin_mcp.py --stdio")
    sys.exit(1)


if __name__ == "__main__":
    main()
