"""HTTP client for the official Timenotes Platform API (https://docs.timenotes.io).

Auth is a single ``ApiToken`` header (Timenotes > Settings > Profile); the
token is scoped to one workspace, so there is no login or account switching.
Bodies are wrapped by resource name (``{"time_log": {...}}``), lists come back
as ``{"<resources>": [...], "meta": {"pagination": {...}}}``, max 200 per page.
Nested query params use Rails brackets: ``filters[from]``, ``filters[x][]``.

Verified live (2026-11):
  * ``start_at`` has a ``Z`` suffix but is the local wall-clock time, so
    ``start_at[:10]`` is the local date.
  * ``/time_logs`` honours only date filters; ``/reports/detailed`` also takes
    project/client/member filters, but only as ``hash_id`` values (UUIDs
    silently match nothing).
  * Single-day ranges (``from == to``) work.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import httpx
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_BASE_URL = "https://platform.timenotes.io/v2"
MAX_PER_PAGE = 200


class TimenotesError(ToolError):
    """Raised when the Timenotes API returns a non-2xx response.

    A ``ToolError`` so the MCP SDK shows the message to the model; any other
    exception type is reduced to a generic "Error executing tool".
    """

    def __init__(self, status: int, body: Any):
        hint = " (Timenotes API token is missing or invalid)" if status == 401 else ""
        super().__init__(f"Timenotes API error {status}: {body!r}{hint}")
        self.status = status
        self.body = body


class TimenotesClient:
    def __init__(
        self,
        api_token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        time_zone: str | None = None,
        timeout: float = 30.0,
    ):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.time_zone = time_zone
        self._http = httpx.Client(timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json", "ApiToken": self.api_token or ""}
        if self.time_zone:
            headers["TimeZone"] = self.time_zone
        for attempt in range(4):
            resp = self._http.request(
                method, f"{self.base_url}{path}",
                headers=headers, params=flatten_params(params), json=json,
            )
            if resp.status_code != 429 or attempt == 3:
                break
            # Sync tools run on a worker thread, so this sleep doesn't stall the server.
            time.sleep(_retry_after(resp))
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise TimenotesError(resp.status_code, body)
        if not resp.content:
            return None
        return resp.json()

    def list_all(
        self, path: str, key: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """GET every page of a list endpoint and return the ``key`` items."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.request(
                "GET", path, params={**(params or {}), "per_page": MAX_PER_PAGE, "page": page}
            ) or {}
            out.extend(data.get(key) or [])
            pages = ((data.get("meta") or {}).get("pagination") or {}).get("total_pages") or 0
            if page >= pages:
                return out
            page += 1


def flatten_params(params: Mapping[str, Any] | None, prefix: str = "") -> list[tuple[str, str]]:
    """Nested dict -> Rails-style query pairs; ``None`` values are dropped."""
    out: list[tuple[str, str]] = []
    for k, v in (params or {}).items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if v is None:
            continue
        if isinstance(v, Mapping):
            out += flatten_params(v, key)
        elif isinstance(v, (list, tuple)):
            out += [(f"{key}[]", str(x)) for x in v]
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        else:
            out.append((key, str(v)))
    return out


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After", "2")), 0.5), 10.0)
    except ValueError:
        return 2.0
