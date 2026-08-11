#!/usr/bin/env python3
"""
LinkedIn MCP Server - Client for Docker Background Server.
"""
import asyncio
import json
import sys
import httpx

MCP_SERVER_NAME = "linkedin-analyzer"
MCP_SERVER_VERSION = "1.0.0"
BACKGROUND_URL = "http://127.0.0.1:8766"

async def http_call(method: str, path: str) -> dict:
    async with httpx.AsyncClient(base_url=BACKGROUND_URL, timeout=120.0) as client:
        if method == "GET":
            r = await client.get(path)
        elif method == "POST":
            r = await client.post(path)
        elif method == "DELETE":
            r = await client.delete(path)
        else:
            return {"error": f"Unknown method: {method}"}
        return r.json()

async def handle_message(message: dict) -> dict | None:
    method = message.get("method", "")
    params = message.get("params", {})
    msg_id = message.get("id")
    
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
            },
        }
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "tools": [
                    {"name": "linkedin_get_profile", "description": "Get LinkedIn profile data.", "inputSchema": {"type": "object", "properties": {}}},
                    {"name": "linkedin_check_session", "description": "Check login status.", "inputSchema": {"type": "object", "properties": {}}},
                    {"name": "linkedin_login", "description": "Open browser at LinkedIn login. Browser STAYS OPEN.", "inputSchema": {"type": "object", "properties": {}}},
                    {"name": "linkedin_save_session", "description": "Save session.", "inputSchema": {"type": "object", "properties": {}}},
                    {"name": "linkedin_analyze_profile", "description": "Get profile with analysis prompt.", "inputSchema": {"type": "object", "properties": {}}},
                    {"name": "linkedin_clear_session", "description": "Clear session.", "inputSchema": {"type": "object", "properties": {}}},
                ],
            },
        }
    elif method == "tools/call":
        name = params.get("name", "")
        if name == "linkedin_get_profile":
            result = await http_call("GET", "/profile")
        elif name == "linkedin_check_session":
            result = await http_call("GET", "/check")
        elif name == "linkedin_login":
            result = await http_call("POST", "/login")
        elif name == "linkedin_save_session":
            result = await http_call("POST", "/save")
        elif name == "linkedin_analyze_profile":
            result = await http_call("GET", "/profile")
        elif name == "linkedin_clear_session":
            result = await http_call("DELETE", "/session")
        else:
            result = {"error": f"Unknown tool: {name}"}
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]},
        }
    elif method == "notifications/initialized":
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

async def run_stdio_server():
    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), flush=True)
    try:
        while True:
            line = sys.stdin.readline()
            if not line: break
            try:
                message = json.loads(line.strip())
                response = await handle_message(message)
                if response: print(json.dumps(response, ensure_ascii=False), flush=True)
            except json.JSONDecodeError as e:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {e}"}}), flush=True)
    except Exception as e:
        print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Internal error: {e}"}}), flush=True)

async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--stdio":
        await run_stdio_server()
    else:
        print("Usage: python linkedin_mcp.py --stdio")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
