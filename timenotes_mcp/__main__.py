"""Entry point for ``timenotes-mcp`` (and ``python -m timenotes_mcp``).

Two transports:

* ``stdio`` (default) — local subprocess, the way Claude Desktop / Claude Code
  / Hermes spawn an MCP server. Credentials come from env / ``.secrets``.

* ``http`` — Streamable HTTP transport with an OAuth 2.0 wrapper. For
  self-hosting on a domain so Claude.ai can connect to it as a custom
  connector. Credentials are entered through the web login form (Timenotes
  email + password).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .server import auto_login_from_env, mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="timenotes-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.getenv("TIMENOTES_MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio).",
    )
    parser.add_argument("--host", default=os.getenv("TIMENOTES_MCP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("TIMENOTES_MCP_PORT", "8000")),
    )
    parser.add_argument(
        "--public-url",
        default=os.getenv("TIMENOTES_MCP_PUBLIC_URL", ""),
        help="Public HTTPS URL (e.g. https://timenotes-mcp.example.com). "
             "Required for --transport=http.",
    )
    parser.add_argument(
        "--state-dir",
        default=os.getenv("TIMENOTES_MCP_STATE_DIR", "/var/lib/timenotes-mcp"),
        help="Directory for SQLite + encryption key (http transport only).",
    )
    parser.add_argument(
        "--log-level", default=os.getenv("TIMENOTES_MCP_LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.transport == "stdio":
        auto_login_from_env()
        mcp.run()
        return

    # http transport
    if not args.public_url:
        print("ERROR: --public-url (or TIMENOTES_MCP_PUBLIC_URL) is required for http transport.",
              file=sys.stderr)
        sys.exit(2)

    import uvicorn

    from .http_app import build_app
    from .oauth import OAuthStore, load_or_create_encryption_key

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    key = load_or_create_encryption_key(state_dir)
    store = OAuthStore(state_dir / "oauth.sqlite3", key)
    store.purge_expired()

    app = build_app(public_url=args.public_url, state_dir=state_dir, store=store)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        # Trust X-Forwarded-* from the reverse proxy (Plesk / Cloudflare).
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
