"""Starlette ASGI app for the remote MCP transport.

Adds an OAuth 2.0 authorization-server in front of the FastMCP Streamable HTTP
endpoint. The login form authenticates the user against Timenotes itself —
that's the only credential we ever ask for.

Layout:
    /                                   -> redirect to /authorize
    /.well-known/oauth-authorization-server  -> OAuth metadata
    /.well-known/oauth-protected-resource    -> resource metadata (RFC 9728)
    /register                           -> Dynamic Client Registration (RFC 7591)
    /authorize          (GET)           -> render login form
    /authorize          (POST)          -> validate Timenotes creds, redirect with code
    /token              (POST)          -> exchange code for access token
    /mcp/*                              -> Streamable HTTP MCP endpoint (Bearer-protected)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .client import TimenotesClient, TimenotesError
from .oauth import OAuthStore, parse_basic_auth, split_scope, _random_token  # noqa: F401
from .server import mcp, set_session


log = logging.getLogger("timenotes_mcp.http")


# ---------------------------------------------------------------------------
# config / startup
# ---------------------------------------------------------------------------

class HttpConfig:
    """Resolved at startup from env + cli flags. Immutable after that."""

    def __init__(
        self,
        *,
        public_url: str,
        state_dir: Path,
        store: OAuthStore,
    ):
        self.public_url = public_url.rstrip("/")
        self.state_dir = state_dir
        self.store = store
        # Templates folder
        self.template_dir = Path(__file__).resolve().parent

    def issuer(self) -> str:
        return self.public_url

    def resource_url(self) -> str:
        return f"{self.public_url}/mcp"

    def render_login(self, **ctx: Any) -> str:
        """Tiny template engine — replaces ``{{ key }}`` and ``{% if %}`` blocks."""
        html = (self.template_dir / "login.html").read_text(encoding="utf-8")
        # Handle {% if error %}...{% endif %}
        if not ctx.get("error"):
            import re
            html = re.sub(
                r"{% if error %}.*?{% endif %}",
                "",
                html,
                flags=re.DOTALL,
            )
        else:
            html = html.replace("{% if error %}", "").replace("{% endif %}", "")
        for k, v in ctx.items():
            html = html.replace(f"{{{{ {k} }}}}", _html_escape(str(v) if v is not None else ""))
        return html


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# OAuth endpoints
# ---------------------------------------------------------------------------

async def _well_known_auth_server(request: Request) -> Response:
    cfg: HttpConfig = request.app.state.cfg
    issuer = cfg.issuer()
    return JSONResponse({
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "registration_endpoint": f"{issuer}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp"],
    })


async def _well_known_protected_resource(request: Request) -> Response:
    cfg: HttpConfig = request.app.state.cfg
    return JSONResponse({
        "resource": cfg.resource_url(),
        "authorization_servers": [cfg.issuer()],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


async def _register(request: Request) -> Response:
    cfg: HttpConfig = request.app.state.cfg
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise HTTPException(400, "redirect_uris is required")

    client = cfg.store.register_client(
        client_name=body.get("client_name") or body.get("client_id") or "Unknown",
        redirect_uris=redirect_uris,
        grant_types=body.get("grant_types"),
        response_types=body.get("response_types"),
        token_endpoint_auth_method=body.get("token_endpoint_auth_method") or "none",
    )
    log.info("Registered OAuth client: id=%s name=%s", client.client_id, client.client_name)
    metadata = client.to_metadata()
    return JSONResponse(metadata, status_code=201)


# --- /authorize: render form (GET) and accept submission (POST) ------------

# In-memory map: state_token -> {client_id, redirect_uri, state, code_challenge,
#                                code_challenge_method, scope, expires_at}
# Used to remember the originating Claude.ai params while the user fills the
# login form. State tokens are short-lived random strings.
_PENDING_AUTH: dict[str, dict[str, Any]] = {}
_PENDING_TTL = 10 * 60


def _purge_pending() -> None:
    now = time.time()
    for tok in [k for k, v in _PENDING_AUTH.items() if v["expires_at"] < now]:
        _PENDING_AUTH.pop(tok, None)


async def _authorize_get(request: Request) -> Response:
    cfg: HttpConfig = request.app.state.cfg
    qs = request.query_params

    client_id = qs.get("client_id")
    redirect_uri = qs.get("redirect_uri")
    response_type = qs.get("response_type") or "code"
    state = qs.get("state")
    code_challenge = qs.get("code_challenge")
    code_challenge_method = qs.get("code_challenge_method")
    scope = qs.get("scope") or "mcp"

    if response_type != "code":
        raise HTTPException(400, "Only response_type=code is supported.")
    if not client_id or not redirect_uri:
        raise HTTPException(400, "client_id and redirect_uri are required.")

    client = cfg.store.get_client(client_id)
    if not client:
        raise HTTPException(400, "Unknown client_id (register via /register first).")
    if redirect_uri not in client.redirect_uris:
        raise HTTPException(400, "redirect_uri does not match any registered URI.")

    _purge_pending()
    state_token = _random_token(24)
    _PENDING_AUTH[state_token] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "expires_at": time.time() + _PENDING_TTL,
    }

    html = cfg.render_login(
        form_action="/authorize",
        client_name=client.client_name,
        state_token=state_token,
        error=None,
    )
    return HTMLResponse(html)


async def _authorize_post(request: Request) -> Response:
    cfg: HttpConfig = request.app.state.cfg
    form = await request.form()

    state_token = form.get("state_token")
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""

    pending = _PENDING_AUTH.get(state_token or "")
    if not pending or pending["expires_at"] < time.time():
        raise HTTPException(400, "Authorization session expired — restart the flow.")

    if not email or not password:
        return _render_error(cfg, pending, state_token, "Email and password are required.")

    # Authenticate against Timenotes itself.
    client = cfg.store.get_client(pending["client_id"])
    if not client:
        raise HTTPException(400, "Unknown client_id.")

    tn = TimenotesClient()
    try:
        tn.login(email, password)
    except TimenotesError as exc:
        msg = "Invalid Timenotes credentials." if exc.status == 401 else f"Timenotes error: {exc.body}"
        return _render_error(cfg, pending, state_token, msg)

    # Store the Timenotes session token (encrypted) and push it into the live
    # MCP client so subsequent tool calls work.
    cfg.store.save_timenotes_session(
        token=tn.access_token or "",
        account_id=tn.account_id,
        email=email,
    )
    set_session(access_token=tn.access_token, account_id=tn.account_id, user=tn.user)

    # Issue an authorization code, redirect back to the OAuth client.
    code = cfg.store.create_code(
        client_id=pending["client_id"],
        redirect_uri=pending["redirect_uri"],
        code_challenge=pending.get("code_challenge"),
        code_challenge_method=pending.get("code_challenge_method"),
        scope=pending.get("scope"),
    )
    _PENDING_AUTH.pop(state_token, None)

    redirect_params = {"code": code}
    if pending.get("state"):
        redirect_params["state"] = pending["state"]
    redirect_url = pending["redirect_uri"]
    sep = "&" if urlsplit(redirect_url).query else "?"
    return RedirectResponse(f"{redirect_url}{sep}{urlencode(redirect_params)}", status_code=303)


def _render_error(cfg: HttpConfig, pending: dict, state_token: str, error: str) -> Response:
    client = cfg.store.get_client(pending["client_id"])
    name = client.client_name if client else pending["client_id"]
    html = cfg.render_login(
        form_action="/authorize",
        client_name=name,
        state_token=state_token,
        error=error,
    )
    return HTMLResponse(html, status_code=400)


# --- /token: exchange code for access token --------------------------------

async def _token(request: Request) -> Response:
    cfg: HttpConfig = request.app.state.cfg
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type"}, status_code=400,
        )
    code = form.get("code")
    redirect_uri = form.get("redirect_uri")
    code_verifier = form.get("code_verifier")
    client_id = form.get("client_id")

    # Allow client_id via Basic auth for confidential clients.
    basic = parse_basic_auth(request.headers.get("authorization"))
    if basic:
        client_id = client_id or basic[0]

    if not all([code, redirect_uri, client_id]):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    consumed = cfg.store.consume_code(
        code=code, client_id=client_id, redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    if not consumed:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token, expires_in = cfg.store.issue_token(
        client_id=client_id, scope=consumed.get("scope"),
    )
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": consumed.get("scope") or "mcp",
    })


# ---------------------------------------------------------------------------
# Bearer auth middleware for /mcp
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject /mcp requests without a valid bearer token."""

    PROTECTED_PREFIX = "/mcp"

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(self.PROTECTED_PREFIX):
            return await call_next(request)

        cfg: HttpConfig = request.app.state.cfg
        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return _challenge(cfg)

        token = auth.split(" ", 1)[1].strip()
        record = cfg.store.lookup_token(token)
        if not record:
            return _challenge(cfg)

        # Ensure the in-memory MCP client has the saved Timenotes session.
        # (Restart safety: process may have lost the in-memory session.)
        from .server import get_client
        if not get_client().access_token:
            saved = cfg.store.load_timenotes_session()
            if saved:
                set_session(
                    access_token=saved["token"],
                    account_id=saved.get("account_id"),
                )

        return await call_next(request)


def _challenge(cfg: HttpConfig) -> Response:
    return JSONResponse(
        {"error": "invalid_token"},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="timenotes-mcp", '
                f'resource_metadata="{cfg.public_url}/.well-known/oauth-protected-resource"'
            ),
        },
    )


# ---------------------------------------------------------------------------
# Root + healthcheck
# ---------------------------------------------------------------------------

async def _root(request: Request) -> Response:
    cfg: HttpConfig = request.app.state.cfg
    return JSONResponse({
        "name": "timenotes-mcp",
        "mcp_endpoint": cfg.resource_url(),
        "oauth_authorization_server": f"{cfg.issuer()}/.well-known/oauth-authorization-server",
    })


async def _health(_: Request) -> Response:
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Build the ASGI app
# ---------------------------------------------------------------------------

def build_app(*, public_url: str, state_dir: Path, store: OAuthStore) -> Starlette:
    """Construct the Starlette app combining OAuth + MCP."""
    cfg = HttpConfig(public_url=public_url, state_dir=state_dir, store=store)

    # On startup, restore any saved Timenotes session into the in-memory client.
    saved = store.load_timenotes_session()
    if saved:
        set_session(access_token=saved["token"], account_id=saved.get("account_id"))
        log.info("Restored Timenotes session for %s", saved.get("email"))

    # FastMCP gives us a Streamable HTTP ASGI app. We mount it under /mcp.
    mcp.settings.streamable_http_path = "/"
    mcp.settings.json_response = False
    mcp.settings.stateless_http = True
    mcp_app = mcp.streamable_http_app()

    # Forward the mcp app's lifespan into ours, otherwise the MCP session
    # manager's task group never starts and every /mcp request 500s with
    # "Task group is not initialized".
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    routes = [
        Route("/", _root, methods=["GET"]),
        Route("/healthz", _health, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", _well_known_auth_server, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", _well_known_protected_resource, methods=["GET"]),
        Route("/register", _register, methods=["POST"]),
        Route("/authorize", _authorize_get, methods=["GET"]),
        Route("/authorize", _authorize_post, methods=["POST"]),
        Route("/token", _token, methods=["POST"]),
        Mount("/mcp", app=mcp_app),
    ]

    app = Starlette(
        routes=routes,
        middleware=[Middleware(BearerAuthMiddleware)],
        lifespan=lifespan,
    )
    app.state.cfg = cfg
    return app
