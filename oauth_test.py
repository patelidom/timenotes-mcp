"""End-to-end test of the OAuth + remote MCP flow.

Spawns the HTTP server in a background thread, then runs a fake Claude.ai
client through the full handshake:
  1. POST /register             — Dynamic Client Registration
  2. GET  /authorize            — render login form
  3. POST /authorize            — submit Timenotes creds
  4. POST /token                — exchange code for access token
  5. POST /mcp/                 — initialize + tools/list using the bearer token

The Timenotes credentials come from .secrets so the login is real.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets_mod
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from timenotes_mcp.http_app import build_app
from timenotes_mcp.oauth import OAuthStore, load_or_create_encryption_key


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main() -> None:
    email = os.environ["TIMENOTES_EMAIL"]
    password = os.environ["TIMENOTES_PASSWORD"]

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    state_dir = Path(tempfile.mkdtemp(prefix="timenotes-mcp-oauth-test-"))
    print(f"state dir: {state_dir}  port: {port}")

    store = OAuthStore(state_dir / "oauth.sqlite3",
                       load_or_create_encryption_key(state_dir))
    app = build_app(public_url=base, state_dir=state_dir, store=store)

    # Use uvicorn's Server class so we can shut it down cleanly.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to come up.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/healthz", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    try:
        with httpx.Client(timeout=10, follow_redirects=False) as c:
            # 1. Discovery
            meta = c.get(f"{base}/.well-known/oauth-authorization-server").json()
            print(f"[discovery] issuer={meta['issuer']}")
            assert meta["authorization_endpoint"].endswith("/authorize")

            # 2. Dynamic Client Registration
            redirect_uri = "https://claude.ai/api/mcp/auth_callback"
            reg = c.post(f"{base}/register", json={
                "client_name": "Claude (test)",
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
            })
            reg.raise_for_status()
            client_id = reg.json()["client_id"]
            print(f"[register] client_id={client_id}")

            # 3. Authorize (GET) — get the form, parse the state_token
            verifier = _b64url(_secrets_mod.token_bytes(48))
            challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
            r = c.get(f"{base}/authorize", params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "rnd-state-123",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "mcp",
            })
            r.raise_for_status()
            html = r.text
            # Extract state_token from the hidden input.
            import re
            m = re.search(r'name="state_token" value="([^"]+)"', html)
            assert m, "could not find state_token in login form"
            state_token = m.group(1)
            print(f"[authorize-get] state_token={state_token[:8]}…")

            # 4. Authorize (POST) — submit Timenotes credentials
            r = c.post(f"{base}/authorize", data={
                "state_token": state_token,
                "email": email,
                "password": password,
            })
            assert r.status_code == 303, f"expected 303 redirect, got {r.status_code}: {r.text[:200]}"
            location = r.headers["location"]
            print(f"[authorize-post] redirect={location[:80]}…")
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(location).query)
            code = qs["code"][0]
            assert qs["state"][0] == "rnd-state-123"

            # 5. Token exchange
            r = c.post(f"{base}/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "client_id": client_id,
            })
            r.raise_for_status()
            access_token = r.json()["access_token"]
            print(f"[token] access_token={access_token[:8]}…  expires_in={r.json()['expires_in']}")

            # 6. MCP call — initialize then tools/list
            mcp_url = f"{base}/mcp/"
            mcp_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            init = c.post(mcp_url, headers=mcp_headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "oauth-test", "version": "0.1"},
                },
            })
            init.raise_for_status()
            print(f"[mcp/initialize] status={init.status_code}")

            # Send the initialized notification (Streamable HTTP requires the
            # session id from the response header for follow-up calls).
            session_id = init.headers.get("mcp-session-id") or init.headers.get("Mcp-Session-Id")
            if session_id:
                mcp_headers["mcp-session-id"] = session_id
            c.post(mcp_url, headers=mcp_headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })

            # tools/list
            r = c.post(mcp_url, headers=mcp_headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })
            r.raise_for_status()
            text = r.text
            # Streamable HTTP can return JSON or SSE. Parse both.
            if text.startswith("event:") or "data:" in text:
                # SSE: pull the first data: line
                payload = next(
                    (line[5:].strip() for line in text.splitlines() if line.startswith("data:")),
                    "{}",
                )
                tools_resp = json.loads(payload)
            else:
                tools_resp = r.json()
            tool_count = len(tools_resp.get("result", {}).get("tools", []))
            print(f"[mcp/tools/list] tools={tool_count}")

            # Bonus: timenotes_whoami — confirms the saved session is in use.
            r = c.post(mcp_url, headers=mcp_headers, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "timenotes_whoami", "arguments": {}},
            })
            r.raise_for_status()
            txt = r.text
            if "data:" in txt:
                payload = next(
                    (line[5:].strip() for line in txt.splitlines() if line.startswith("data:")),
                    "{}",
                )
                call_resp = json.loads(payload)
            else:
                call_resp = r.json()
            content = call_resp.get("result", {}).get("content", [])
            print(f"[mcp/whoami] content_blocks={len(content)}")
            print(f"  preview: {(content[0].get('text','') if content else '')[:120]}")

            print("\nALL OAUTH + MCP CHECKS PASSED")

    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
