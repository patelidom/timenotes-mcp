"""MCP server exposing the official Timenotes Platform API as tools.

A handful of dedicated tools cover the daily work (time logs, tracker,
lookups, reports, aggregations); ``timenotes_api`` reaches every other
endpoint of https://docs.timenotes.io directly.

Auth: ``TIMENOTES_API_TOKEN`` env (stdio) or the token saved by the OAuth
login form (http transport, see ``http_app``).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import DEFAULT_BASE_URL, MAX_PER_PAGE, TimenotesClient, TimenotesError

TIME_ZONE = os.getenv("TIMENOTES_TIMEZONE", "Europe/Prague")

mcp = MCPServer(
    name="timenotes",
    version="0.2.0",
    instructions=(
        "Timenotes.io time tracking. Conventions used by every tool:\n"
        "- Dates are YYYY-MM-DD; times of day (start_at) are local HH:MM; all durations are in MINUTES.\n"
        "- start_at in responses ends with 'Z' but is already LOCAL time — do not shift it.\n"
        "- To log time, prefer timenotes_log_time(project, task, duration): it resolves "
        "project/task by NAME, creates the task if missing, and defaults date/start to now. "
        "Use timenotes_create_time_log only when you already have the UUIDs.\n"
        "- IDs are UUIDs from the list tools (timenotes_list_projects, timenotes_list_tasks, "
        "timenotes_list_clients, timenotes_list_members).\n"
        "- Aggregations (timenotes_time_per_client/project/task/day) return minutes and hours.\n"
        "- Anything without a dedicated tool (project/client/tag CRUD, absences, approvals, "
        "comments, saved reports, ...) goes through timenotes_api.\n"
        "- If a call fails with 'Not authenticated' or 401, the server has no valid Timenotes API "
        "token — tell the user; do not guess credentials.\n"
        "- Deletes are irreversible; confirm with the user first."
    ),
)

_client = TimenotesClient(
    api_token=os.getenv("TIMENOTES_API_TOKEN") or None,
    base_url=os.getenv("TIMENOTES_BASE_URL", DEFAULT_BASE_URL),
    time_zone=TIME_ZONE,
)


def set_session(*, api_token: str | None) -> None:
    """Install the API token (used by the HTTP transport after OAuth login)."""
    _client.api_token = api_token


def get_client() -> TimenotesClient:
    return _client


def _require_auth() -> None:
    if not _client.api_token:
        raise ToolError(
            "Not authenticated: no Timenotes API token. Set TIMENOTES_API_TOKEN in the "
            "MCP server env, or re-authorize the connector with the token from "
            "Timenotes > Settings > Profile."
        )


def _api(method: str, path: str, *, params: dict[str, Any] | None = None,
         json: dict[str, Any] | None = None) -> Any:
    _require_auth()
    return _client.request(method, path, params=params, json=json)


def _all(path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    _require_auth()
    return _client.list_all(path, key, params)


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# --- who / lookups ----------------------------------------------------------

@mcp.tool()
def timenotes_whoami() -> dict[str, Any]:
    """Return the current user and their membership in the token's workspace."""
    return {
        "user": (_api("GET", "/users/current") or {}).get("user"),
        "member": (_api("GET", "/users_accounts/current") or {}).get("users_account"),
        "time_zone": TIME_ZONE,
    }


@mcp.tool()
def timenotes_list_projects() -> dict[str, Any]:
    """List active projects: id, hash_id, name and client name."""
    return {"projects": [_compact_project(p) for p in _all("/projects", "projects")]}


def _compact_project(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p.get("id"),
        "hash_id": p.get("hash_id"),
        "name": p.get("name"),
        "client": (p.get("client") or {}).get("name"),
    }


@mcp.tool()
def timenotes_list_tasks(project_id: str, search: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List active tasks of a project (projects can have hundreds).

    ``search`` filters by case-insensitive substring of the task name — prefer
    it over paging through everything. At most ``limit`` tasks are returned;
    ``total`` tells you how many matched.
    """
    if limit < 0:
        raise ToolError("limit must be non-negative")
    tasks = _all(f"/projects/{project_id}/tasks", "tasks")
    if search:
        needle = search.lower()
        tasks = [t for t in tasks if needle in (t.get("name") or "").lower()]
    out: dict[str, Any] = {"total": len(tasks), "tasks": [_compact_task(t) for t in tasks[:limit]]}
    if len(tasks) > limit:
        out["note"] = f"Showing {limit} of {len(tasks)} tasks — narrow down with `search` or raise `limit`."
    return out


def _compact_task(t: dict[str, Any]) -> dict[str, Any]:
    """Drop null/empty noise fields so big task lists stay readable."""
    keep = {"id": t.get("id"), "name": t.get("name"), "state": t.get("state")}
    for k in ("description", "time_estimate_duration", "is_billable", "bookmarked", "recently_tracked", "tags"):
        v = t.get(k)
        if v not in (None, "", [], False):
            keep[k] = v
    return keep


@mcp.tool()
def timenotes_list_clients() -> dict[str, Any]:
    """List clients in the workspace."""
    return {"clients": _all("/clients", "clients")}


@mcp.tool()
def timenotes_list_tags() -> dict[str, Any]:
    """List tags in the workspace."""
    return {"tags": _all("/tags", "tags")}


@mcp.tool()
def timenotes_list_members() -> dict[str, Any]:
    """List workspace members (users_accounts)."""
    return {"users_accounts": _all("/users_accounts", "users_accounts")}


# --- active tracker ---------------------------------------------------------

@mcp.tool()
def timenotes_get_active_tracker() -> dict[str, Any]:
    """Return the running tracker, or ``{"active_tracker": null}`` if none."""
    try:
        return _api("GET", "/active_tracker") or {"active_tracker": None}
    except TimenotesError as exc:
        if exc.status == 404:
            return {"active_tracker": None}
        raise


@mcp.tool()
def timenotes_start_tracker(
    task_id: str,
    description: str | None = None,
    started_at: str | None = None,
    is_billable: bool | None = None,
    tag_ids: list[str] | None = None,
) -> Any:
    """Start a tracker on a task. ``started_at`` is ISO 8601; defaults to now.

    Fails if another tracker is already running — stop it first.
    """
    body = _drop_none({"task_id": task_id, "description": description,
                       "started_at": started_at, "is_billable": is_billable, "tags": tag_ids})
    return _api("POST", "/active_tracker", json={"active_tracker": body})


@mcp.tool()
def timenotes_update_active_tracker(
    task_id: str | None = None,
    description: str | None = None,
    is_billable: bool | None = None,
) -> Any:
    """Change the running tracker's task, description or billable flag."""
    body = _drop_none({"task_id": task_id, "description": description, "is_billable": is_billable})
    if not body:
        raise ToolError("Provide at least one field to update on the tracker.")
    return _api("PATCH", "/active_tracker", json={"active_tracker": body})


@mcp.tool()
def timenotes_stop_tracker() -> dict[str, Any]:
    """Stop the running tracker and turn it into a time log."""
    _api("DELETE", "/active_tracker")
    return {"ok": True}


# --- time logs --------------------------------------------------------------

@mcp.tool()
def timenotes_list_time_logs(
    from_date: str | None = None,
    to_date: str | None = None,
    per_page: int = 100,
    page: int = 1,
) -> Any:
    """List your time logs between two dates (``YYYY-MM-DD``, inclusive, both optional).

    Paginated (max 200 per page) — check ``meta.pagination.total_pages``.
    To filter by project/client/member use ``timenotes_report_detailed``.
    """
    return _api("GET", "/time_logs", params={
        "filters": {"from": from_date, "to": to_date},
        "per_page": min(per_page, MAX_PER_PAGE), "page": page,
    })


@mcp.tool()
def timenotes_create_time_log(
    project_id: str,
    task_id: str,
    date: str,
    start_at: str,
    duration: int,
    description: str | None = None,
    is_billable: bool | None = None,
) -> Any:
    """Create a time log: ``date`` ``YYYY-MM-DD``, ``start_at`` local ``HH:MM``, ``duration`` in minutes."""
    body = _drop_none({"project_id": project_id, "task_id": task_id, "date": date,
                       "start_at": start_at, "duration": duration,
                       "description": description, "is_billable": is_billable})
    return _api("POST", "/time_logs", json={"time_log": body})


@mcp.tool()
def timenotes_log_time(
    project: str,
    task: str,
    duration: int,
    start_at: str | None = None,
    date: str | None = None,
    description: str | None = None,
    create_task_if_missing: bool = True,
) -> dict[str, Any]:
    """Log time in one call, resolving ``project`` and ``task`` by NAME.

    ``duration`` is in minutes; ``date`` (``YYYY-MM-DD``) and ``start_at``
    (local ``HH:MM``) default to now. A case-insensitive exact name match wins,
    else a unique substring match; ambiguous or unknown names fail with the
    candidates instead of guessing. A missing task is created when
    ``create_task_if_missing`` is set.
    """
    if duration <= 0:
        raise ToolError("duration must be a positive number of minutes")
    proj = _resolve_one(_all("/projects", "projects"), project, kind="project")
    pid = proj["id"]

    match = _match_by_name(_all(f"/projects/{pid}/tasks", "tasks"), task)
    created_task = False
    if len(match) == 1:
        tid = match[0]["id"]
    elif len(match) > 1:
        raise ToolError(_ambiguous_msg("task", task, match))
    elif create_task_if_missing:
        tid = _api("POST", f"/projects/{pid}/tasks", json={"task": {"name": task}})["task"]["id"]
        created_task = True
    else:
        raise ToolError(f"No active task named {task!r} in project {proj.get('name')!r}.")

    now = datetime.now(ZoneInfo(TIME_ZONE))
    log = timenotes_create_time_log(
        project_id=pid, task_id=tid,
        date=date or now.strftime("%Y-%m-%d"),
        start_at=start_at or now.strftime("%H:%M"),
        duration=duration, description=description,
    )
    return {
        "ok": True,
        "created_task": created_task,
        "project": {"id": pid, "name": proj.get("name")},
        "task": {"id": tid, "name": match[0]["name"] if match else task},
        "time_log": (log or {}).get("time_log"),
    }


@mcp.tool()
def timenotes_update_time_log(
    time_log_id: str,
    description: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    date: str | None = None,
    start_at: str | None = None,
    duration: int | None = None,
    is_billable: bool | None = None,
) -> dict[str, Any]:
    """Patch a time log; only non-null arguments are sent.

    ``date`` ``YYYY-MM-DD``, ``start_at`` local ``HH:MM`` (pass ``date`` with it),
    ``duration`` in minutes.
    """
    body = _drop_none({"description": description, "project_id": project_id, "task_id": task_id,
                       "date": date, "start_at": start_at, "duration": duration,
                       "is_billable": is_billable})
    if not body:
        raise ToolError("Provide at least one field to update.")
    _api("PATCH", f"/time_logs/{time_log_id}", json={"time_log": body})
    return {"ok": True, "updated_id": time_log_id, "fields": body}


@mcp.tool()
def timenotes_delete_time_log(time_log_id: str) -> dict[str, Any]:
    """Delete a time log by id (irreversible)."""
    _api("DELETE", f"/time_logs/{time_log_id}")
    return {"ok": True, "deleted_id": time_log_id}


# --- reports & aggregations -------------------------------------------------

@mcp.tool()
def timenotes_report_detailed(
    from_date: str,
    to_date: str,
    project_ids: list[str] | None = None,
    client_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
    task_name: str | None = None,
    description: str | None = None,
    grouping: str | None = None,
    per_page: int = 100,
    page: int = 1,
) -> Any:
    """Detailed report rows + ``totals`` (worktime in minutes) for a date range.

    Filters take UUIDs from the list tools (converted to hash_ids for you);
    ``task_name`` / ``description`` are partial matches. ``grouping`` is one of
    ``user``, ``project``, ``client``, ``task`` — rows then become per-group
    totals. Paginated, max 200 per page.
    """
    params: dict[str, Any] = {
        "filters": _report_filters(from_date, to_date, project_ids=project_ids,
                                   client_ids=client_ids, user_ids=user_ids,
                                   task_name=task_name, description=description),
        "per_page": min(per_page, MAX_PER_PAGE), "page": page,
    }
    if grouping:
        params["grouping"] = {"primary": grouping}
    return _api("GET", "/reports/detailed", params=params)


@mcp.tool()
def timenotes_time_per_client(from_date: str, to_date: str) -> dict[str, Any]:
    """Minutes/hours per client in the date range, largest first (name null = no client)."""
    rows = _grouped("client", from_date, to_date)
    return {"from": from_date, "to": to_date, "count": len(rows), "clients": rows}


@mcp.tool()
def timenotes_time_per_project(from_date: str, to_date: str) -> dict[str, Any]:
    """Minutes/hours per project in the date range, largest first."""
    rows = _grouped("project", from_date, to_date)
    return {"from": from_date, "to": to_date, "count": len(rows), "projects": rows}


@mcp.tool()
def timenotes_time_per_task(from_date: str, to_date: str, project_id: str | None = None) -> dict[str, Any]:
    """Minutes/hours per task in the date range, optionally for one project."""
    rows = _grouped("task", from_date, to_date, project_ids=[project_id] if project_id else None)
    return {"from": from_date, "to": to_date, "project_id": project_id, "count": len(rows), "tasks": rows}


@mcp.tool()
def timenotes_time_per_day(from_date: str, to_date: str) -> dict[str, Any]:
    """Minutes/hours per day in the date range."""
    logs = _all("/time_logs", "time_logs", {"filters": {"from": from_date, "to": to_date}})
    days: dict[str, int] = {}
    for log in logs:
        day = (log.get("start_at") or "")[:10] or "unknown"  # local date, see client.py
        days[day] = days.get(day, 0) + int(log.get("duration") or 0)
    rows = [{"date": d, "duration_minutes": m, "duration_hours": round(m / 60, 2)}
            for d, m in sorted(days.items())]
    return {"from": from_date, "to": to_date, "count": len(rows), "days": rows}


def _report_filters(from_date: str | None, to_date: str | None, *, project_ids=None,
                    client_ids=None, user_ids=None, **extra: Any) -> dict[str, Any]:
    return _drop_none({
        "from": from_date, "to": to_date,
        "project_hash_ids": _to_hash_ids(project_ids, "/projects", "projects"),
        "client_hash_ids": _to_hash_ids(client_ids, "/clients", "clients"),
        "users_account_hash_ids": _to_hash_ids(user_ids, "/users_accounts", "users_accounts"),
        **extra,
    })


def _to_hash_ids(ids: list[str] | None, path: str, key: str) -> list[str] | None:
    """UUIDs -> hash_ids (report filters only accept hash_ids); unknown values pass through."""
    if not ids:
        return None
    by_id = {x.get("id"): x.get("hash_id") for x in _all(path, key)}
    return [by_id.get(i) or i for i in ids]


def _grouped(primary: str, from_date: str, to_date: str, **filters: Any) -> list[dict[str, Any]]:
    rows = _all("/reports/detailed", "rows", {
        "filters": _report_filters(from_date, to_date, **filters),
        "grouping": {"primary": primary},
    })
    out = []
    for r in rows:
        mins = int(r.get("duration") or 0)
        row = {"id": r.get("id"), "name": r.get("name") or (
            f"{r.get('first_name', '')} {r.get('last_name', '')}".strip() or None)}
        if isinstance(r.get("project"), dict):
            row["project"] = r["project"].get("name")
        row.update(duration_minutes=mins, duration_hours=round(mins / 60, 2))
        out.append(row)
    out.sort(key=lambda x: x["duration_minutes"], reverse=True)
    return out


# --- tasks ------------------------------------------------------------------

@mcp.tool()
def timenotes_create_task(project_id: str, name: str, fields: dict[str, Any] | None = None) -> Any:
    """Create a task on a project. Extra API fields (``description``, ``is_billable``) go in ``fields``."""
    return _api("POST", f"/projects/{project_id}/tasks", json={"task": {"name": name, **(fields or {})}})


@mcp.tool()
def timenotes_update_task(project_id: str, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Patch a task, e.g. ``fields={"name": "..."}``."""
    if not fields:
        raise ToolError("Provide at least one field to update.")
    return _api("PATCH", f"/projects/{project_id}/tasks/{task_id}", json={"task": fields}) or {"ok": True}


# --- everything else --------------------------------------------------------

_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


@mcp.tool()
def timenotes_api(
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call any Timenotes API endpoint — for everything the dedicated tools don't cover.

    ``path`` is relative to https://platform.timenotes.io/v2 (e.g. ``/clients``).
    ``query`` nests Rails-style: ``{"filters": {"from": "2026-01-01"}}`` ->
    ``filters[from]=2026-01-01``. ``body`` is wrapped by resource, e.g.
    ``{"project": {"name": "X", "client_id": "..."}}``. Lists take
    ``page``/``per_page`` (max 200). Reference: https://docs.timenotes.io

    Endpoints ({id} = UUID):
      /clients GET POST · /clients/{id} PATCH DELETE
      /projects GET POST · /projects/{id} GET PATCH DELETE · /projects/{id}/copy POST
      /projects/{pid}/tasks GET POST · /projects/{pid}/tasks/{id} GET PATCH DELETE
      /projects/{pid}/tasks/{id}/assign PATCH
      /projects/{pid}/tasks/{tid}/comments POST · .../comments/{id} PATCH DELETE
      /projects/{pid}/projects_users GET POST · .../projects_users/{id} PATCH DELETE
      /projects/{pid}/projects_groups GET POST · .../projects_groups/{id} DELETE
      /tags /alerts /members_groups /saved_reports GET POST · /<same>/{id} PATCH DELETE
      /saved_reports/{id} GET · /contacts GET POST · /contacts/{id} DELETE
      /holidays/absences /holidays/absence_types /holidays/free_days GET POST · /{id} PATCH DELETE
      /holidays/absence_requests GET · /{id} PATCH DELETE · /{id}/approve PATCH · /{id}/reject PATCH
      /approvals/periods GET · /approvals/periods/{id} PATCH · /approvals/periods/fetch?date= GET
      /approvals/periods/requests GET · /approvals/periods/{id}/requests GET
      /approvals/periods/{id}/requests/bulk_apply POST · /approvals/periods/{id}/requests/user_summary GET
      /active_trackers/{id}/stop DELETE (another member's tracker)
      /activities/dashboard GET · /billing_info GET POST
      /users/current GET · /users_accounts GET · /users_accounts/current GET · /users_accounts/{id} GET

    DELETE and bulk_apply are irreversible — confirm with the user first.
    """
    method = method.upper()
    if method not in _METHODS:
        raise ToolError(f"method must be one of {sorted(_METHODS)}")
    if path.startswith("/v2/"):
        path = path[3:]
    # Trust boundary: a leading '/' keeps the request (and the token) on the API host.
    if not path.startswith("/") or "://" in path:
        raise ToolError("path must be relative to the API root and start with '/', e.g. '/clients'")
    return _api(method, path, params=query, json=body) or {"ok": True}


# --- name resolution helpers ------------------------------------------------

def _match_by_name(items: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Exact (case-insensitive) matches if any, else substring matches."""
    needle = name.strip().lower()
    exact = [it for it in items if (it.get("name") or "").strip().lower() == needle]
    return exact or [it for it in items if needle in (it.get("name") or "").lower()]


def _ambiguous_msg(kind: str, name: str, candidates: list[dict[str, Any]]) -> str:
    listed = ", ".join(f"{c.get('name')!r} (id {c.get('id')})" for c in candidates[:10])
    return f"{kind.capitalize()} name {name!r} is ambiguous — matches: {listed}. Retry with the exact name."


def _resolve_one(items: list[dict[str, Any]], name: str, *, kind: str) -> dict[str, Any]:
    match = _match_by_name(items, name)
    if len(match) == 1:
        return match[0]
    if not match:
        raise ToolError(f"No {kind} named {name!r} found.")
    raise ToolError(_ambiguous_msg(kind, name, match))
