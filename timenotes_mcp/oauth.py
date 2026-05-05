"""OAuth 2.0 authorization-server bits used to gate the remote MCP transport.

Single-user model: the only "user" is whoever knows the Timenotes credentials,
so the OAuth login page IS the Timenotes login. We don't keep a user table.

Storage is a single SQLite file with three tables:

* ``oauth_clients``  — clients registered via Dynamic Client Registration (RFC 7591)
* ``oauth_codes``    — short-lived authorization codes (PKCE-protected)
* ``oauth_tokens``   — bearer access tokens issued to those clients

The Timenotes session token is encrypted at rest with a key from the
``TIMENOTES_OAUTH_SECRET`` env var (or auto-generated on first run and saved
next to the DB). Without the key the DB is useless.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets_mod
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken


CODE_TTL_SECONDS = 5 * 60               # auth codes live 5 minutes
TOKEN_TTL_SECONDS = 24 * 60 * 60        # bearer tokens live 24 hours

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_secret TEXT,
    client_name TEXT,
    redirect_uris TEXT NOT NULL,
    grant_types TEXT,
    response_types TEXT,
    token_endpoint_auth_method TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT,
    code_challenge_method TEXT,
    scope TEXT,
    expires_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    access_token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    scope TEXT,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS timenotes_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    encrypted_token BLOB NOT NULL,
    account_id TEXT,
    email TEXT,
    updated_at INTEGER NOT NULL
);
"""


def _now() -> int:
    return int(time.time())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _random_token(nbytes: int = 32) -> str:
    return _b64url(_secrets_mod.token_bytes(nbytes))


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify a PKCE code_verifier against the stored challenge (S256 or plain)."""
    if not code_verifier:
        return False
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return _b64url(digest) == code_challenge
    if method == "plain" or not method:
        return code_verifier == code_challenge
    return False


@dataclass
class Client:
    client_id: str
    client_secret: str | None
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    token_endpoint_auth_method: str

    def to_metadata(self) -> dict[str, Any]:
        out = {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "redirect_uris": self.redirect_uris,
            "grant_types": self.grant_types,
            "response_types": self.response_types,
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
        }
        if self.client_secret:
            out["client_secret"] = self.client_secret
        return out


class OAuthStore:
    """All OAuth + Timenotes session state lives here."""

    def __init__(self, db_path: str | os.PathLike[str], encryption_key: bytes):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(encryption_key)
        self._init_schema()

    # ------------------------------------------------------------------
    # connection helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # clients (Dynamic Client Registration)
    # ------------------------------------------------------------------

    def register_client(
        self,
        *,
        client_name: str,
        redirect_uris: list[str],
        grant_types: list[str] | None = None,
        response_types: list[str] | None = None,
        token_endpoint_auth_method: str = "none",
    ) -> Client:
        client = Client(
            client_id=f"tn-mcp-{uuid.uuid4().hex[:16]}",
            client_secret=None if token_endpoint_auth_method == "none" else _random_token(32),
            client_name=client_name or "(unnamed client)",
            redirect_uris=list(redirect_uris),
            grant_types=list(grant_types or ["authorization_code", "refresh_token"]),
            response_types=list(response_types or ["code"]),
            token_endpoint_auth_method=token_endpoint_auth_method,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO oauth_clients VALUES (?,?,?,?,?,?,?,?)",
                (
                    client.client_id, client.client_secret, client.client_name,
                    json.dumps(client.redirect_uris),
                    json.dumps(client.grant_types),
                    json.dumps(client.response_types),
                    client.token_endpoint_auth_method,
                    _now(),
                ),
            )
        return client

    def get_client(self, client_id: str) -> Client | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if not row:
            return None
        return Client(
            client_id=row["client_id"],
            client_secret=row["client_secret"],
            client_name=row["client_name"],
            redirect_uris=json.loads(row["redirect_uris"]),
            grant_types=json.loads(row["grant_types"] or "[]"),
            response_types=json.loads(row["response_types"] or "[]"),
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
        )

    # ------------------------------------------------------------------
    # auth codes
    # ------------------------------------------------------------------

    def create_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str | None,
        code_challenge_method: str | None,
        scope: str | None,
    ) -> str:
        code = _random_token(32)
        with self._conn() as c:
            c.execute(
                "INSERT INTO oauth_codes VALUES (?,?,?,?,?,?,?,0)",
                (
                    code, client_id, redirect_uri, code_challenge,
                    code_challenge_method, scope,
                    _now() + CODE_TTL_SECONDS,
                ),
            )
        return code

    def consume_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> dict[str, Any] | None:
        """Atomically consume an authorization code. Returns the stored row or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM oauth_codes WHERE code = ? AND used = 0", (code,)
            ).fetchone()
            if not row:
                return None
            if row["client_id"] != client_id:
                return None
            if row["redirect_uri"] != redirect_uri:
                return None
            if row["expires_at"] < _now():
                return None
            if row["code_challenge"]:
                if not verify_pkce(
                    code_verifier or "",
                    row["code_challenge"],
                    row["code_challenge_method"] or "plain",
                ):
                    return None
            c.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))
        return dict(row)

    # ------------------------------------------------------------------
    # access tokens
    # ------------------------------------------------------------------

    def issue_token(self, *, client_id: str, scope: str | None) -> tuple[str, int]:
        token = _random_token(32)
        expires_at = _now() + TOKEN_TTL_SECONDS
        with self._conn() as c:
            c.execute(
                "INSERT INTO oauth_tokens VALUES (?,?,?,?,0,?)",
                (token, client_id, scope, expires_at, _now()),
            )
        return token, TOKEN_TTL_SECONDS

    def lookup_token(self, access_token: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM oauth_tokens WHERE access_token = ? AND revoked = 0",
                (access_token,),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] < _now():
            return None
        return dict(row)

    def revoke_token(self, access_token: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE access_token = ?",
                (access_token,),
            )

    # ------------------------------------------------------------------
    # Timenotes session (single row, encrypted)
    # ------------------------------------------------------------------

    def save_timenotes_session(
        self, *, token: str, account_id: str | None, email: str | None
    ) -> None:
        encrypted = self._fernet.encrypt(token.encode("utf-8"))
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO timenotes_session VALUES (1,?,?,?,?)",
                (encrypted, account_id, email, _now()),
            )

    def load_timenotes_session(self) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM timenotes_session WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        try:
            token = self._fernet.decrypt(row["encrypted_token"]).decode("utf-8")
        except InvalidToken:
            return None
        return {
            "token": token,
            "account_id": row["account_id"],
            "email": row["email"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------

    def purge_expired(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (_now(),))
            c.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (_now(),))


def load_or_create_encryption_key(state_dir: Path) -> bytes:
    """Read the Fernet key from env, otherwise read/create a key file in ``state_dir``."""
    env_key = os.getenv("TIMENOTES_OAUTH_SECRET")
    if env_key:
        try:
            Fernet(env_key.encode())
            return env_key.encode()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "TIMENOTES_OAUTH_SECRET is set but is not a valid Fernet key. "
                "Generate one with: python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            ) from exc
    key_path = state_dir / "secret.key"
    if key_path.exists():
        return key_path.read_bytes().strip()
    state_dir.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    return key


def parse_basic_auth(header_value: str | None) -> tuple[str, str] | None:
    if not header_value or not header_value.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header_value[6:]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    if ":" not in raw:
        return None
    user, _, pw = raw.partition(":")
    return user, pw


def split_scope(scope: str | None) -> list[str]:
    return [s for s in (scope or "").split() if s]


def join_scope(scopes: Iterable[str]) -> str:
    return " ".join(scopes)
