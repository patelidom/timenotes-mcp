"""FastMCP server exposing Timenotes.io time-tracker operations as tools.

Works with any MCP-compatible client (Hermes Agent, Claude Desktop, Claude Code,
etc). The server holds a single authenticated ``TimenotesClient`` in process
memory for the lifetime of the subprocess.

Credentials can be supplied either at startup via environment variables
(``TIMENOTES_EMAIL``/``TIMENOTES_PASSWORD``, optional ``TIMENOTES_ACCOUNT_ID``)
or at runtime via the ``timenotes_login`` tool.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import DEFAULT_BASE_URL, TimenotesClient, TimenotesError

mcp = FastMCP(name="timenotes")

_client = TimenotesClient(
    base_url=os.getenv("TIMENOTES_BASE_URL", DEFAULT_BASE_URL),
    access_token=os.getenv("TIMENOTES_TOKEN") or None,
    account_id=os.getenv("TIMENOTES_ACCOUNT_ID") or None,
)


def _require_auth() -> None:
    if not _client.access_token:
        raise RuntimeError(
            "Not authenticated. Call timenotes_login first, or set TIMENOTES_EMAIL"
            " and TIMENOTES_PASSWORD (or TIMENOTES_TOKEN) in the MCP server env."
        )


def auto_login_from_env() -> None:
    """Auto-login using env vars. Called by the entry point, not on import."""
    if _client.access_token:
        return
    email = os.getenv("TIMENOTES_EMAIL")
    password = os.getenv("TIMENOTES_PASSWORD")
    if email and password:
        try:
            _client.login(email, password)
        except TimenotesError:
            # Surface at first tool call; don't crash the server on startup.
            pass


# --- session ---------------------------------------------------------------

@mcp.tool()
def timenotes_login(email: str, password: str, account_id: str | None = None) -> dict[str, Any]:
    """Authenticate with Timenotes and cache the token for subsequent calls.

    Normally you don't need this — credentials can be supplied via environment
    variables at server startup. Use this tool only if env vars aren't set or
    you want to switch accounts mid-session.
    """
    data = _client.login(email, password)
    if account_id:
        _client.set_account(account_id)
    return {
        "ok": True,
        "account_id": _client.account_id,
        "raw": _safe(data),
    }


@mcp.tool()
def timenotes_logout() -> dict[str, Any]:
    """End the current Timenotes session."""
    _require_auth()
    _client.logout()
    return {"ok": True}


@mcp.tool()
def timenotes_set_account(account_id: str) -> dict[str, Any]:
    """Switch the active Timenotes workspace/account for subsequent calls."""
    _client.set_account(account_id)
    return {"ok": True, "account_id": _client.account_id}


@mcp.tool()
def timenotes_whoami() -> dict[str, Any]:
    """Return the current user and the currently selected account/workspace."""
    _require_auth()
    return {
        "user": _safe(_client.current_user()),
        "account": _safe(_client.current_account()),
        "account_id": _client.account_id,
    }


@mcp.tool()
def timenotes_list_accounts() -> Any:
    """List all Timenotes accounts (workspaces) the current user belongs to."""
    _require_auth()
    return _safe(_client.list_accounts())


# --- lookups ---------------------------------------------------------------

@mcp.tool()
def timenotes_list_projects(include_archived: bool = True) -> Any:
    """List projects in the current account. By default includes archived ones."""
    _require_auth()
    return _safe(_client.list_projects(all_=include_archived))


@mcp.tool()
def timenotes_list_tasks(project_id: str) -> Any:
    """List tasks for a given project."""
    _require_auth()
    return _safe(_client.list_tasks(project_id))


@mcp.tool()
def timenotes_list_tags() -> Any:
    """List tags available in the current account."""
    _require_auth()
    return _safe(_client.list_tags())


@mcp.tool()
def timenotes_list_clients() -> Any:
    """List clients in the current workspace (v2)."""
    _require_auth()
    return _safe(_client.list_clients())


@mcp.tool()
def timenotes_list_members() -> Any:
    """List members (user accounts) in the current workspace."""
    _require_auth()
    return _safe(_client.list_members())


# --- active tracker --------------------------------------------------------

@mcp.tool()
def timenotes_get_active_tracker() -> Any:
    """Return the currently running tracker (if any)."""
    _require_auth()
    return _safe(_client.get_active_tracker())


@mcp.tool()
def timenotes_start_tracker(
    project_id: str,
    task_id: str | None = None,
    description: str | None = None,
    tag_ids: list[str] | None = None,
    started_at: str | None = None,
    time_zone: str | None = None,
) -> Any:
    """Start a new running tracker. ``started_at`` is ISO 8601; defaults to now server-side.

    Fails if another tracker is already running — stop it first.
    """
    _require_auth()
    tracker: dict[str, Any] = {"project_id": project_id}
    if task_id is not None:
        tracker["task_id"] = task_id
    if description is not None:
        tracker["description"] = description
    if tag_ids:
        tracker["tag_ids"] = tag_ids
    if started_at is not None:
        tracker["started_at"] = started_at
    if time_zone is not None:
        tracker["time_zone"] = time_zone
    return _safe(_client.start_tracker(tracker))


@mcp.tool()
def timenotes_stop_tracker() -> dict[str, Any]:
    """Stop the currently running tracker and convert it into a time log."""
    _require_auth()
    _client.stop_tracker()
    return {"ok": True}


# --- time logs -------------------------------------------------------------

@mcp.tool()
def timenotes_list_time_logs(
    from_date: str | None = None,
    to_date: str | None = None,
    per_page: int = 100,
) -> Any:
    """List time logs. Dates are ISO 8601 (``YYYY-MM-DD``). Both ends optional."""
    _require_auth()
    return _safe(_client.list_time_logs(from_date=from_date, to_date=to_date, per_page=per_page))


@mcp.tool()
def timenotes_create_time_log(
    project_id: str,
    task_id: str,
    date: str,
    start_at: str,
    duration: int,
    description: str | None = None,
    tag_ids: list[str] | None = None,
) -> Any:
    """Create a time log.

    Required: ``project_id``, ``task_id``, ``date`` (``YYYY-MM-DD``),
    ``start_at`` (``HH:MM`` local time), ``duration`` **in minutes**.
    """
    _require_auth()
    body: dict[str, Any] = {
        "project_id": project_id,
        "task_id": task_id,
        "date": date,
        "start_at": start_at,
        "duration": duration,
    }
    if description is not None:
        body["description"] = description
    if tag_ids:
        body["tag_ids"] = tag_ids
    return _safe(_client.create_time_log(body))


@mcp.tool()
def timenotes_update_time_log(
    time_log_id: str,
    description: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    date: str | None = None,
    start_at: str | None = None,
    duration: int | None = None,
    tag_ids: list[str] | None = None,
) -> Any:
    """Patch fields on an existing time log. Only non-null arguments are sent.

    ``date`` is ``YYYY-MM-DD``; ``start_at`` is ``HH:MM``; ``duration`` is in **minutes**.
    """
    _require_auth()
    body: dict[str, Any] = {}
    if description is not None:
        body["description"] = description
    if project_id is not None:
        body["project_id"] = project_id
    if task_id is not None:
        body["task_id"] = task_id
    if date is not None:
        body["date"] = date
    if start_at is not None:
        body["start_at"] = start_at
    if duration is not None:
        body["duration"] = duration
    if tag_ids is not None:
        body["tag_ids"] = tag_ids
    if not body:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_time_log(time_log_id, body))


@mcp.tool()
def timenotes_delete_time_log(time_log_id: str) -> dict[str, Any]:
    """Delete a time log by id."""
    _require_auth()
    _client.delete_time_log(time_log_id)
    return {"ok": True, "deleted_id": time_log_id}


# --- reports ---------------------------------------------------------------

@mcp.tool()
def timenotes_report_detailed(
    from_date: str | None = None,
    to_date: str | None = None,
    project_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
) -> Any:
    """Fetch the detailed report. Date range and optional project/user filters."""
    _require_auth()
    return _safe(_client.report_detailed(_report_params(from_date, to_date, project_ids, user_ids)))


@mcp.tool()
def timenotes_report_chart(
    from_date: str | None = None,
    to_date: str | None = None,
    project_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
) -> Any:
    """Fetch the detailed-report chart series for the same filters as the report."""
    _require_auth()
    return _safe(_client.report_detailed_chart(_report_params(from_date, to_date, project_ids, user_ids)))


@mcp.tool()
def timenotes_report_export_columns() -> Any:
    """List the columns available for the detailed-report export."""
    _require_auth()
    return _safe(_client.report_export_columns())


@mcp.tool()
def timenotes_export_report_detailed(
    from_date: str,
    to_date: str,
    columns: list[str],
    type: str = "csv",
    output_dir: str = "/tmp",
) -> dict[str, Any]:
    """Export the detailed report to a file (csv / xlsx / pdf).

    ``columns`` must be non-empty — call ``timenotes_report_export_columns``
    first to discover the names you can pick from. The file is written under
    ``output_dir`` and the returned dict contains ``path``, ``size_bytes``,
    and ``content_type``.
    """
    _require_auth()
    download = _client.export_report_detailed(
        from_date=from_date, to_date=to_date, columns=columns, type=type,
    )
    return _save_download(download, output_dir)


@mcp.tool()
def timenotes_export_timesheet(
    from_date: str,
    to_date: str,
    type: str = "csv",
    output_dir: str = "/tmp",
) -> dict[str, Any]:
    """Export the timesheet grid to a file (csv / xlsx / pdf).

    Writes under ``output_dir`` and returns ``path``, ``size_bytes``,
    and ``content_type``.
    """
    _require_auth()
    download = _client.export_timesheet(from_date=from_date, to_date=to_date, type=type)
    return _save_download(download, output_dir)


def _save_download(download: dict[str, Any], output_dir: str) -> dict[str, Any]:
    import os as _os
    _os.makedirs(output_dir, exist_ok=True)
    filename = download.get("filename") or "timenotes-download.bin"
    path = _os.path.join(output_dir, filename)
    with open(path, "wb") as f:
        f.write(download["content"])
    return {
        "path": path,
        "size_bytes": download["size_bytes"],
        "content_type": download.get("content_type"),
        "filename": filename,
    }


def _report_params(
    from_date: str | None,
    to_date: str | None,
    project_ids: list[str] | None,
    user_ids: list[str] | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if project_ids:
        params["project_ids"] = ",".join(project_ids)
    if user_ids:
        params["user_ids"] = ",".join(user_ids)
    return params


# --- timesheets ------------------------------------------------------------

@mcp.tool()
def timenotes_get_timesheet(
    from_date: str | None = None,
    to_date: str | None = None,
    user_ids: list[str] | None = None,
) -> Any:
    """Fetch the timesheet grid for the given range and (optional) users."""
    _require_auth()
    params: dict[str, Any] = {}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if user_ids:
        params["user_ids"] = ",".join(user_ids)
    return _safe(_client.get_timesheet(params))


# --- bulk operations -------------------------------------------------------

@mcp.tool()
def timenotes_bulk_modify_time_logs(payload: dict[str, Any]) -> Any:
    """Apply a bulk modification to selected time logs.

    The body is forwarded as-is — typically: ``{"ids": [...], "time_log": {...}}``.
    Use carefully, this hits multiple records at once.
    """
    _require_auth()
    return _safe(_client.bulk_modify_time_logs(payload))


@mcp.tool()
def timenotes_bulk_remove_time_logs(ids: list[str]) -> Any:
    """Bulk-delete time logs by id list."""
    _require_auth()
    return _safe(_client.bulk_remove_time_logs({"ids": ids}))


@mcp.tool()
def timenotes_bulk_copy_time_logs(payload: dict[str, Any]) -> Any:
    """Duplicate selected time logs (e.g. copy to another date).

    Body is forwarded as-is — typically: ``{"ids": [...], "date": "YYYY-MM-DD"}``.
    """
    _require_auth()
    return _safe(_client.bulk_copy_time_logs(payload))


# --- aggregate analytics ---------------------------------------------------

@mcp.tool()
def timenotes_time_per_client(from_date: str, to_date: str) -> dict[str, Any]:
    """Hours logged per client across the date range, sorted by total descending."""
    _require_auth()
    rows = _client.time_per_client(from_date=from_date, to_date=to_date)
    return {"from": from_date, "to": to_date, "count": len(rows), "clients": rows}


@mcp.tool()
def timenotes_time_per_project(from_date: str, to_date: str) -> dict[str, Any]:
    """Hours logged per project across the date range, sorted by total descending."""
    _require_auth()
    rows = _client.time_per_project(from_date=from_date, to_date=to_date)
    return {"from": from_date, "to": to_date, "count": len(rows), "projects": rows}


@mcp.tool()
def timenotes_time_per_task(
    from_date: str, to_date: str, project_id: str | None = None
) -> dict[str, Any]:
    """Hours logged per task; optionally filter to a single project."""
    _require_auth()
    rows = _client.time_per_task(from_date=from_date, to_date=to_date, project_id=project_id)
    return {"from": from_date, "to": to_date, "project_id": project_id,
            "count": len(rows), "tasks": rows}


@mcp.tool()
def timenotes_time_per_day(from_date: str, to_date: str) -> dict[str, Any]:
    """Hours logged per day across the range."""
    _require_auth()
    rows = _client.time_per_day(from_date=from_date, to_date=to_date)
    return {"from": from_date, "to": to_date, "count": len(rows), "days": rows}


# --- clients CRUD ----------------------------------------------------------

@mcp.tool()
def timenotes_get_client(client_id: str) -> Any:
    """Read a single client."""
    _require_auth()
    return _safe(_client.get_client(client_id))


@mcp.tool()
def timenotes_create_client(name: str, **fields: Any) -> Any:
    """Create a new client. Pass any extra fields the API supports as kwargs."""
    _require_auth()
    body = {"name": name, **fields}
    return _safe(_client.create_client(body))


@mcp.tool()
def timenotes_update_client(client_id: str, **fields: Any) -> Any:
    """Patch fields on an existing client."""
    _require_auth()
    if not fields:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_client(client_id, fields))


@mcp.tool()
def timenotes_delete_client(client_id: str) -> dict[str, Any]:
    """Delete a client by id."""
    _require_auth()
    _client.delete_client(client_id)
    return {"ok": True, "deleted_id": client_id}


# --- projects CRUD ---------------------------------------------------------

@mcp.tool()
def timenotes_get_project(project_id: str) -> Any:
    """Read a single project (v2)."""
    _require_auth()
    return _safe(_client.get_project(project_id))


@mcp.tool()
def timenotes_create_project(name: str, client_id: str | None = None, **fields: Any) -> Any:
    """Create a new project. Optionally attach to a client."""
    _require_auth()
    body: dict[str, Any] = {"name": name, **fields}
    if client_id is not None:
        body["client_id"] = client_id
    return _safe(_client.create_project(body))


@mcp.tool()
def timenotes_update_project(project_id: str, **fields: Any) -> Any:
    """Patch fields on a project (e.g. ``name``, ``client_id``, ``color``)."""
    _require_auth()
    if not fields:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_project(project_id, fields))


@mcp.tool()
def timenotes_delete_project(project_id: str) -> dict[str, Any]:
    """Delete a project."""
    _require_auth()
    _client.delete_project(project_id)
    return {"ok": True, "deleted_id": project_id}


# --- tasks CRUD ------------------------------------------------------------

@mcp.tool()
def timenotes_get_task(project_id: str, task_id: str) -> Any:
    """Read a single task (v2)."""
    _require_auth()
    return _safe(_client.get_task(project_id, task_id))


@mcp.tool()
def timenotes_create_task(project_id: str, name: str, **fields: Any) -> Any:
    """Create a task on a project."""
    _require_auth()
    body = {"name": name, **fields}
    return _safe(_client.create_task(project_id, body))


@mcp.tool()
def timenotes_update_task(project_id: str, task_id: str, **fields: Any) -> Any:
    """Patch fields on a task."""
    _require_auth()
    if not fields:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_task(project_id, task_id, fields))


@mcp.tool()
def timenotes_delete_task(project_id: str, task_id: str) -> dict[str, Any]:
    """Delete a task."""
    _require_auth()
    _client.delete_task(project_id, task_id)
    return {"ok": True, "deleted_id": task_id}


@mcp.tool()
def timenotes_bookmark_task(project_id: str, task_id: str) -> Any:
    """Bookmark a task (pin it for quick access)."""
    _require_auth()
    return _safe(_client.bookmark_task(project_id, task_id))


@mcp.tool()
def timenotes_unbookmark_task(project_id: str, task_id: str) -> Any:
    """Remove the bookmark from a task."""
    _require_auth()
    return _safe(_client.unbookmark_task(project_id, task_id))


# --- tags CRUD -------------------------------------------------------------

@mcp.tool()
def timenotes_create_tag(name: str, **fields: Any) -> Any:
    """Create a tag."""
    _require_auth()
    return _safe(_client.create_tag({"name": name, **fields}))


@mcp.tool()
def timenotes_update_tag(tag_id: str, **fields: Any) -> Any:
    """Patch fields on a tag."""
    _require_auth()
    if not fields:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_tag(tag_id, fields))


@mcp.tool()
def timenotes_delete_tag(tag_id: str) -> dict[str, Any]:
    """Delete a tag."""
    _require_auth()
    _client.delete_tag(tag_id)
    return {"ok": True, "deleted_id": tag_id}


# --- alerts ----------------------------------------------------------------

@mcp.tool()
def timenotes_list_alerts() -> Any:
    """List alerts (notifications) for the current user."""
    _require_auth()
    return _safe(_client.list_alerts())


@mcp.tool()
def timenotes_update_alert(alert_id: str, **fields: Any) -> Any:
    """Patch an alert (e.g. mark read)."""
    _require_auth()
    return _safe(_client.update_alert(alert_id, fields))


# --- dashboard -------------------------------------------------------------

@mcp.tool()
def timenotes_get_dashboard() -> Any:
    """Workspace dashboard: who is currently active, totals, etc."""
    _require_auth()
    return _safe(_client.get_dashboard())


# --- holidays / absences ---------------------------------------------------

@mcp.tool()
def timenotes_list_absence_requests(
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
) -> Any:
    """List absence (vacation) requests, with optional date range and status filter."""
    _require_auth()
    params: dict[str, Any] = {"per_page": 200}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if status:
        params["status"] = status
    return _safe(_client.list_absence_requests(params))


@mcp.tool()
def timenotes_create_absence_request(
    absence_type_id: str,
    date_from: str,
    date_to: str,
    description: str | None = None,
    **fields: Any,
) -> Any:
    """Create an absence request (e.g. vacation, sick day)."""
    _require_auth()
    body = {"absence_type_id": absence_type_id, "date_from": date_from, "date_to": date_to, **fields}
    if description is not None:
        body["description"] = description
    return _safe(_client.create_absence_request(body))


@mcp.tool()
def timenotes_update_absence_request(request_id: str, **fields: Any) -> Any:
    """Patch fields on an absence request."""
    _require_auth()
    if not fields:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_absence_request(request_id, fields))


@mcp.tool()
def timenotes_delete_absence_request(request_id: str) -> dict[str, Any]:
    """Withdraw / delete an absence request."""
    _require_auth()
    _client.delete_absence_request(request_id)
    return {"ok": True, "deleted_id": request_id}


@mcp.tool()
def timenotes_approve_absence_request(request_id: str) -> Any:
    """Approve an absence request (manager action)."""
    _require_auth()
    return _safe(_client.approve_absence_request(request_id))


@mcp.tool()
def timenotes_reject_absence_request(request_id: str) -> Any:
    """Reject an absence request (manager action)."""
    _require_auth()
    return _safe(_client.reject_absence_request(request_id))


@mcp.tool()
def timenotes_list_absences(
    from_date: str | None = None, to_date: str | None = None
) -> Any:
    """List recorded absences (approved + taken)."""
    _require_auth()
    params: dict[str, Any] = {"per_page": 200}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    return _safe(_client.list_absences(params))


@mcp.tool()
def timenotes_list_absence_types() -> Any:
    """List the absence types configured for this workspace (vacation, sick, etc.)."""
    _require_auth()
    return _safe(_client.list_absence_types())


@mcp.tool()
def timenotes_list_free_days() -> Any:
    """List free / non-working days (public holidays etc.) for the workspace."""
    _require_auth()
    return _safe(_client.list_free_days())


# --- invitations -----------------------------------------------------------

@mcp.tool()
def timenotes_list_invitations() -> Any:
    """List pending invitations."""
    _require_auth()
    return _safe(_client.list_invitations())


@mcp.tool()
def timenotes_invite_member(email: str, **fields: Any) -> Any:
    """Invite a new member by email."""
    _require_auth()
    return _safe(_client.create_invitation({"email": email, **fields}))


@mcp.tool()
def timenotes_bulk_invite_members(emails: list[str], **fields: Any) -> Any:
    """Invite many members at once."""
    _require_auth()
    body = {"emails": emails, **fields}
    return _safe(_client.bulk_create_invitations(body))


@mcp.tool()
def timenotes_delete_invitation(invitation_id: str) -> dict[str, Any]:
    """Cancel a pending invitation."""
    _require_auth()
    _client.delete_invitation(invitation_id)
    return {"ok": True, "deleted_id": invitation_id}


@mcp.tool()
def timenotes_resend_invitation(invitation_id: str) -> Any:
    """Resend an invitation email."""
    _require_auth()
    return _safe(_client.resend_invitation(invitation_id))


# --- members groups --------------------------------------------------------

@mcp.tool()
def timenotes_list_members_groups() -> Any:
    """List member groups (teams)."""
    _require_auth()
    return _safe(_client.list_members_groups())


@mcp.tool()
def timenotes_create_members_group(name: str, **fields: Any) -> Any:
    """Create a member group / team."""
    _require_auth()
    return _safe(_client.create_members_group({"name": name, **fields}))


@mcp.tool()
def timenotes_update_members_group(group_id: str, **fields: Any) -> Any:
    """Patch fields on a member group."""
    _require_auth()
    if not fields:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_members_group(group_id, fields))


@mcp.tool()
def timenotes_delete_members_group(group_id: str) -> dict[str, Any]:
    """Delete a member group."""
    _require_auth()
    _client.delete_members_group(group_id)
    return {"ok": True, "deleted_id": group_id}


# --- integrations ----------------------------------------------------------

@mcp.tool()
def timenotes_list_integrations() -> Any:
    """List integrations connected to this workspace (Basecamp, Asana, etc.)."""
    _require_auth()
    return _safe(_client.list_integrations())


@mcp.tool()
def timenotes_list_available_integrations() -> Any:
    """List integrations available to be connected."""
    _require_auth()
    return _safe(_client.list_available_integrations())


@mcp.tool()
def timenotes_list_integration_accounts() -> Any:
    """List the external accounts linked through integrations."""
    _require_auth()
    return _safe(_client.list_integration_accounts())


# --- settings --------------------------------------------------------------

@mcp.tool()
def timenotes_get_setting() -> Any:
    """Get the workspace settings object."""
    _require_auth()
    return _safe(_client.get_setting())


@mcp.tool()
def timenotes_update_setting(**fields: Any) -> Any:
    """Patch workspace settings."""
    _require_auth()
    if not fields:
        raise ValueError("Provide at least one field to update.")
    return _safe(_client.update_setting(fields))


# --- plans / subscription / storage ----------------------------------------

@mcp.tool()
def timenotes_list_plans() -> Any:
    """List the available subscription plans."""
    _require_auth()
    return _safe(_client.list_plans())


@mcp.tool()
def timenotes_current_subscription_period() -> Any:
    """The current subscription billing period."""
    _require_auth()
    return _safe(_client.current_subscription_period())


@mcp.tool()
def timenotes_list_subscription_periods() -> Any:
    """All subscription billing periods (history)."""
    _require_auth()
    return _safe(_client.list_subscription_periods())


@mcp.tool()
def timenotes_get_storage() -> Any:
    """Workspace storage info / quota."""
    _require_auth()
    return _safe(_client.get_storage())


# --- tracker live edit -----------------------------------------------------

@mcp.tool()
def timenotes_update_active_tracker(
    project_id: str | None = None,
    task_id: str | None = None,
    description: str | None = None,
    tag_ids: list[str] | None = None,
) -> Any:
    """Edit the running tracker (change project/task/description/tags on the fly)."""
    _require_auth()
    body: dict[str, Any] = {}
    if project_id is not None:
        body["project_id"] = project_id
    if task_id is not None:
        body["task_id"] = task_id
    if description is not None:
        body["description"] = description
    if tag_ids is not None:
        body["tag_ids"] = tag_ids
    if not body:
        raise ValueError("Provide at least one field to update on the tracker.")
    return _safe(_client.update_active_tracker(body))


# --- bulk recalc / update rates --------------------------------------------

@mcp.tool()
def timenotes_bulk_recalculate_rates(payload: dict[str, Any]) -> Any:
    """Recalculate billing rates on a set of time logs. Body forwarded as-is."""
    _require_auth()
    return _safe(_client.bulk_recalculate_rates(payload))


@mcp.tool()
def timenotes_bulk_update_rates(payload: dict[str, Any]) -> Any:
    """Update billing rates on a set of time logs. Body forwarded as-is."""
    _require_auth()
    return _safe(_client.bulk_update_rates(payload))


# --- helpers ---------------------------------------------------------------

def _safe(value: Any) -> Any:
    # Convert ``None`` into an empty object so MCP clients always get JSON.
    return value if value is not None else {}
