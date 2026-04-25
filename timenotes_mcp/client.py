"""HTTP client for the Timenotes.io API.

The API is undocumented; endpoints and headers were recovered from the official
Chrome extension service worker. Notable quirks:

  * Auth headers are non-standard: ``AuthorizationToken`` (not Bearer) and
    ``AccountId`` (workspace context, required on every call).
  * Request payloads use snake_case; responses are wrapped by resource name,
    e.g. ``{"time_log": {...}}`` or ``{"projects": [...]}``.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx


DEFAULT_BASE_URL = "https://api.timenotes.io/v1"
V2_BASE_URL = "https://api.timenotes.io/v2"


class TimenotesError(RuntimeError):
    """Raised when the Timenotes API returns a non-2xx response."""

    def __init__(self, status: int, body: Any):
        super().__init__(f"Timenotes API error {status}: {body!r}")
        self.status = status
        self.body = body


class TimenotesClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        access_token: str | None = None,
        account_id: str | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.account_id = account_id
        self.user: dict[str, Any] | None = None
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TimenotesClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            h["AuthorizationToken"] = self.access_token
        if self.account_id:
            h["AccountId"] = str(self.account_id)
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        include_account: bool = True,
        base_url: str | None = None,
    ) -> Any:
        url = f"{(base_url or self.base_url).rstrip('/')}{path}"
        headers = self.headers
        if not include_account:
            headers = {k: v for k, v in headers.items() if k != "AccountId"}
        resp = self._http.request(
            method,
            url,
            headers=headers,
            params=_drop_none(params) if params else None,
            json=json,
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise TimenotesError(resp.status_code, body)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- session ---------------------------------------------------------

    def login(self, email: str, password: str) -> dict[str, Any]:
        """POST /sessions. Stores token + first account id on success."""
        data = self._request(
            "POST",
            "/sessions",
            json={"email": email.lower(), "password": password},
        )
        token = _extract_token(data)
        if not token:
            raise TimenotesError(200, {"message": "login succeeded but no token found", "body": data})
        self.access_token = token
        # Cache the user payload returned by /sessions so we don't have to
        # call a separate /users/current endpoint (which is 404 on live API).
        user = data.get("user") if isinstance(data, Mapping) else None
        if isinstance(user, Mapping):
            self.user = dict(user)
        # Pick a default workspace. /sessions doesn't ship the list, so we
        # fetch it unscoped (without AccountId header) and take the first.
        if not self.account_id:
            try:
                accounts = self.list_accounts()
                first = _first_account_id(accounts)
                if first:
                    self.account_id = str(first)
            except TimenotesError:
                pass
        return data

    def logout(self) -> None:
        self._request("DELETE", "/session")
        self.access_token = None
        self.account_id = None

    def set_account(self, account_id: str) -> None:
        self.account_id = str(account_id)

    # --- users / accounts ------------------------------------------------

    def current_user(self) -> Any:
        """Return the user cached from the last successful login.

        The live ``/users/current`` endpoint 404s, so we rely on the ``user``
        object returned by ``/sessions``.
        """
        return self.user or {}

    def current_account(self) -> Any:
        return self._request("GET", "/users_accounts/current")

    def list_accounts(self) -> Any:
        """List workspaces the user belongs to. Sent without AccountId header."""
        return self._request(
            "GET",
            "/users_accounts",
            params={"per_page": 1001},
            include_account=False,
        )

    # --- lookups ---------------------------------------------------------

    def list_projects(self, *, all_: bool = True) -> Any:
        # /projects/all 404s on live API; use /projects and pass include_archived.
        params: dict[str, Any] = {"per_page": 1000}
        if all_:
            params["include_archived"] = "true"
        return self._request("GET", "/projects", params=params)

    def list_tasks(self, project_id: str | int) -> Any:
        # Tasks listing is only available on v2; v1 404s.
        return self._request(
            "GET",
            f"/projects/{project_id}/tasks",
            params={"per_page": 1000},
            base_url=V2_BASE_URL,
        )

    def list_tags(self) -> Any:
        return self._request("GET", "/tags", params={"per_page": 1000}, base_url=V2_BASE_URL)

    # --- clients (v2) ----------------------------------------------------

    def list_clients(self) -> Any:
        return self._request("GET", "/clients", params={"per_page": 1000}, base_url=V2_BASE_URL)

    def get_client(self, client_id: str | int) -> Any:
        """Read a single client.

        The API has no single-GET endpoint, so we filter ``list_clients``.
        """
        clients = self.list_clients()
        if isinstance(clients, Mapping):
            for c in clients.get("clients", []):
                if isinstance(c, Mapping) and (c.get("id") == client_id or c.get("hash_id") == client_id):
                    return {"client": dict(c)}
        return {}

    def create_client(self, client: Mapping[str, Any]) -> Any:
        return self._request("POST", "/clients", json={"client": dict(client)}, base_url=V2_BASE_URL)

    def update_client(self, client_id: str | int, client: Mapping[str, Any]) -> Any:
        return self._request(
            "PATCH", f"/clients/{client_id}", json={"client": dict(client)}, base_url=V2_BASE_URL
        )

    def delete_client(self, client_id: str | int) -> Any:
        return self._request("DELETE", f"/clients/{client_id}", base_url=V2_BASE_URL)

    # --- members ---------------------------------------------------------

    def list_members(self) -> Any:
        """List members (user accounts) in the current workspace."""
        return self._request("GET", "/users_accounts", params={"per_page": 1000})

    # --- projects (single + CRUD on v2) ----------------------------------

    def get_project(self, project_id: str | int) -> Any:
        return self._request("GET", f"/projects/{project_id}", base_url=V2_BASE_URL)

    def create_project(self, project: Mapping[str, Any]) -> Any:
        return self._request(
            "POST", "/projects", json={"project": dict(project)}, base_url=V2_BASE_URL
        )

    def update_project(self, project_id: str | int, project: Mapping[str, Any]) -> Any:
        return self._request(
            "PATCH", f"/projects/{project_id}",
            json={"project": dict(project)}, base_url=V2_BASE_URL,
        )

    def delete_project(self, project_id: str | int) -> Any:
        return self._request("DELETE", f"/projects/{project_id}", base_url=V2_BASE_URL)

    # --- tasks (CRUD on v2) ----------------------------------------------

    def get_task(self, project_id: str | int, task_id: str | int) -> Any:
        return self._request(
            "GET", f"/projects/{project_id}/tasks/{task_id}", base_url=V2_BASE_URL
        )

    def create_task(self, project_id: str | int, task: Mapping[str, Any]) -> Any:
        return self._request(
            "POST", f"/projects/{project_id}/tasks",
            json={"task": dict(task)}, base_url=V2_BASE_URL,
        )

    def update_task(self, project_id: str | int, task_id: str | int, task: Mapping[str, Any]) -> Any:
        return self._request(
            "PATCH", f"/projects/{project_id}/tasks/{task_id}",
            json={"task": dict(task)}, base_url=V2_BASE_URL,
        )

    def delete_task(self, project_id: str | int, task_id: str | int) -> Any:
        return self._request(
            "DELETE", f"/projects/{project_id}/tasks/{task_id}", base_url=V2_BASE_URL
        )

    def bookmark_task(self, project_id: str | int, task_id: str | int) -> Any:
        return self._request(
            "PATCH", f"/projects/{project_id}/tasks/{task_id}/bookmark", base_url=V2_BASE_URL
        )

    def unbookmark_task(self, project_id: str | int, task_id: str | int) -> Any:
        return self._request(
            "PATCH", f"/projects/{project_id}/tasks/{task_id}/unbookmark", base_url=V2_BASE_URL
        )

    # --- tags (CRUD) -----------------------------------------------------

    def create_tag(self, tag: Mapping[str, Any]) -> Any:
        return self._request("POST", "/tags", json={"tag": dict(tag)}, base_url=V2_BASE_URL)

    def update_tag(self, tag_id: str | int, tag: Mapping[str, Any]) -> Any:
        return self._request("PATCH", f"/tags/{tag_id}", json={"tag": dict(tag)}, base_url=V2_BASE_URL)

    def delete_tag(self, tag_id: str | int) -> Any:
        return self._request("DELETE", f"/tags/{tag_id}", base_url=V2_BASE_URL)

    # --- accounts (v2 alternative) ---------------------------------------

    def list_accounts_v2(self) -> Any:
        return self._request("GET", "/accounts", params={"per_page": 1000}, base_url=V2_BASE_URL)

    def current_account_v2(self) -> Any:
        return self._request("GET", "/accounts/current", base_url=V2_BASE_URL)

    def list_owned_accounts(self) -> Any:
        return self._request("GET", "/users_accounts/owned", base_url=V2_BASE_URL)

    def list_scoped_accounts(self) -> Any:
        return self._request("GET", "/users_accounts/scoped", base_url=V2_BASE_URL)

    # --- alerts ----------------------------------------------------------

    def list_alerts(self) -> Any:
        return self._request("GET", "/alerts", base_url=V2_BASE_URL)

    def update_alert(self, alert_id: str | int, body: Mapping[str, Any]) -> Any:
        return self._request("PATCH", f"/alerts/{alert_id}", json=dict(body), base_url=V2_BASE_URL)

    # --- activities / dashboard ------------------------------------------

    def get_dashboard(self) -> Any:
        return self._request("GET", "/activities/dashboard", base_url=V2_BASE_URL)

    # --- holidays / absences ---------------------------------------------

    def list_absence_requests(self, params: Mapping[str, Any] | None = None) -> Any:
        return self._request(
            "GET", "/holidays/absence_requests",
            params=dict(params or {"per_page": 100}), base_url=V2_BASE_URL,
        )

    def create_absence_request(self, body: Mapping[str, Any]) -> Any:
        return self._request(
            "POST", "/holidays/absence_requests",
            json={"absence_request": dict(body)}, base_url=V2_BASE_URL,
        )

    def update_absence_request(self, request_id: str | int, body: Mapping[str, Any]) -> Any:
        return self._request(
            "PATCH", f"/holidays/absence_requests/{request_id}",
            json={"absence_request": dict(body)}, base_url=V2_BASE_URL,
        )

    def delete_absence_request(self, request_id: str | int) -> Any:
        return self._request(
            "DELETE", f"/holidays/absence_requests/{request_id}", base_url=V2_BASE_URL
        )

    def approve_absence_request(self, request_id: str | int) -> Any:
        return self._request(
            "PATCH", f"/holidays/absence_requests/{request_id}/approve", base_url=V2_BASE_URL
        )

    def reject_absence_request(self, request_id: str | int) -> Any:
        return self._request(
            "PATCH", f"/holidays/absence_requests/{request_id}/reject", base_url=V2_BASE_URL
        )

    def list_absences(self, params: Mapping[str, Any] | None = None) -> Any:
        return self._request(
            "GET", "/holidays/absences",
            params=dict(params or {"per_page": 100}), base_url=V2_BASE_URL,
        )

    def list_absence_types(self) -> Any:
        return self._request(
            "GET", "/holidays/absence_types", params={"per_page": 100}, base_url=V2_BASE_URL
        )

    def list_free_days(self) -> Any:
        return self._request(
            "GET", "/holidays/free_days", params={"per_page": 1000}, base_url=V2_BASE_URL
        )

    # --- invitations -----------------------------------------------------

    def list_invitations(self) -> Any:
        return self._request(
            "GET", "/invitations", params={"per_page": 100}, base_url=V2_BASE_URL
        )

    def create_invitation(self, body: Mapping[str, Any]) -> Any:
        return self._request(
            "POST", "/invitations", json={"invitation": dict(body)}, base_url=V2_BASE_URL
        )

    def bulk_create_invitations(self, body: Mapping[str, Any]) -> Any:
        return self._request(
            "POST", "/invitations/bulk_create", json=dict(body), base_url=V2_BASE_URL
        )

    def delete_invitation(self, invitation_id: str | int) -> Any:
        return self._request(
            "DELETE", f"/invitations/{invitation_id}", base_url=V2_BASE_URL
        )

    def resend_invitation(self, invitation_id: str | int) -> Any:
        return self._request(
            "POST", f"/invitations/{invitation_id}/resend", base_url=V2_BASE_URL
        )

    # --- members groups --------------------------------------------------

    def list_members_groups(self) -> Any:
        return self._request(
            "GET", "/members_groups", params={"per_page": 100}, base_url=V2_BASE_URL
        )

    def create_members_group(self, body: Mapping[str, Any]) -> Any:
        return self._request(
            "POST", "/members_groups", json={"members_group": dict(body)}, base_url=V2_BASE_URL
        )

    def update_members_group(self, group_id: str | int, body: Mapping[str, Any]) -> Any:
        return self._request(
            "PATCH", f"/members_groups/{group_id}",
            json={"members_group": dict(body)}, base_url=V2_BASE_URL,
        )

    def delete_members_group(self, group_id: str | int) -> Any:
        return self._request(
            "DELETE", f"/members_groups/{group_id}", base_url=V2_BASE_URL
        )

    # --- integrations ----------------------------------------------------

    def list_integrations(self) -> Any:
        return self._request("GET", "/integrations", params={"per_page": 100})

    def list_available_integrations(self) -> Any:
        return self._request(
            "GET", "/integrations/available", params={"per_page": 100}, base_url=V2_BASE_URL
        )

    def list_integration_accounts(self) -> Any:
        return self._request(
            "GET", "/integration_accounts", params={"per_page": 100}, base_url=V2_BASE_URL
        )

    # --- settings --------------------------------------------------------

    def get_setting(self) -> Any:
        return self._request("GET", "/setting", base_url=V2_BASE_URL)

    def update_setting(self, body: Mapping[str, Any]) -> Any:
        return self._request("PATCH", "/setting", json={"setting": dict(body)}, base_url=V2_BASE_URL)

    # --- plans / subscription --------------------------------------------

    def list_plans(self) -> Any:
        return self._request("GET", "/plans", params={"per_page": 100}, base_url=V2_BASE_URL)

    def current_subscription_period(self) -> Any:
        return self._request("GET", "/subscription_periods/current", base_url=V2_BASE_URL)

    def list_subscription_periods(self) -> Any:
        return self._request(
            "GET", "/subscription_periods", params={"per_page": 100}, base_url=V2_BASE_URL
        )

    # --- storage ---------------------------------------------------------

    def get_storage(self) -> Any:
        return self._request("GET", "/storage", base_url=V2_BASE_URL)

    # --- tracker live edit -----------------------------------------------

    def update_active_tracker(self, tracker: Mapping[str, Any]) -> Any:
        """PATCH the running tracker (change project/task/description on the fly)."""
        return self._request(
            "PATCH", "/active_tracker",
            json={"active_tracker": dict(tracker)}, base_url=V2_BASE_URL,
        )

    # --- active tracker --------------------------------------------------

    def get_active_tracker(self) -> Any:
        """Return the running tracker, or ``None`` if no tracker is active.

        Reads from v1 (works); the API responds 404 when nothing is tracking,
        which is normalised here.
        """
        try:
            return self._request("GET", "/active_tracker")
        except TimenotesError as exc:
            if exc.status == 404:
                return None
            raise

    def start_tracker(self, tracker: Mapping[str, Any]) -> Any:
        # POST is v2-only; v1 returns 404.
        return self._request(
            "POST",
            "/active_tracker",
            json={"active_tracker": dict(tracker)},
            base_url=V2_BASE_URL,
        )

    def stop_tracker(self) -> Any:
        # DELETE works on either version; default to v2 for consistency.
        return self._request("DELETE", "/active_tracker", base_url=V2_BASE_URL)

    # --- time logs -------------------------------------------------------

    def list_time_logs(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        per_page: int = 100,
    ) -> Any:
        return self._request(
            "GET",
            "/time_logs",
            params={"from": from_date, "to": to_date, "per_page": per_page},
        )

    def create_time_log(self, time_log: Mapping[str, Any]) -> Any:
        """Create a time log.

        Required fields per the API: ``project_id``, ``task_id``, ``date``
        (``YYYY-MM-DD``), ``start_at`` (``HH:MM`` or ISO datetime), ``duration``
        in **minutes**. Description and ``tag_ids`` optional.
        """
        return self._request("POST", "/time_logs", json={"time_log": dict(time_log)})

    def update_time_log(self, time_log_id: str | int, time_log: Mapping[str, Any]) -> Any:
        return self._request("PATCH", f"/time_logs/{time_log_id}", json={"time_log": dict(time_log)})

    def delete_time_log(self, time_log_id: str | int) -> Any:
        return self._request("DELETE", f"/time_logs/{time_log_id}")

    # --- reports (v2) ----------------------------------------------------

    def report_detailed(self, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", "/reports/detailed", params=params or {}, base_url=V2_BASE_URL)

    def report_detailed_chart(self, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", "/reports/detailed/chart", params=params or {}, base_url=V2_BASE_URL)

    def report_export_columns(self, params: Mapping[str, Any] | None = None) -> Any:
        return self._request(
            "GET", "/reports/detailed/export_columns", params=params or {}, base_url=V2_BASE_URL
        )

    # --- exports (binary file responses) ---------------------------------

    def export_timesheet(
        self,
        *,
        from_date: str,
        to_date: str,
        type: str = "csv",
        project_ids: list[str] | None = None,
        user_ids: list[str] | None = None,
        client_ids: list[str] | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET ``/timesheets/export`` — returns a downloaded file.

        ``type`` is ``csv``, ``xlsx`` or ``pdf``. Filter args go through the
        ``filters[*]`` bracket-style query params (Rails convention).
        """
        params: list[tuple[str, str]] = [
            ("filters[from]", from_date),
            ("filters[to]", to_date),
            ("export[type]", type),
        ]
        for pid in project_ids or []:
            params.append(("filters[project_ids][]", pid))
        for uid in user_ids or []:
            params.append(("filters[user_ids][]", uid))
        for cid in client_ids or []:
            params.append(("filters[client_ids][]", cid))
        if extra_params:
            for k, v in extra_params.items():
                params.append((k, str(v)))
        return self._download(
            "GET", "/timesheets/export", params=params, default_name=f"timenotes-timesheet.{type}",
        )

    def export_report_detailed(
        self,
        *,
        from_date: str,
        to_date: str,
        columns: list[str],
        type: str = "csv",
        project_ids: list[str] | None = None,
        user_ids: list[str] | None = None,
        client_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        tag_ids: list[str] | None = None,
        timespan: str = "custom",
        extra_filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST ``/reports/detailed/export`` — returns a downloaded file.

        The request body must match a very specific shape recovered from the
        web UI's network traffic:

        * filter object lives under ``filters`` AND its key fields (``from``,
          ``to``, ``project_hash_ids``, ``timespan``) are **also duplicated at
          the top level** — the server reads both;
        * dates use ``DD/MM/YYYY`` (not ISO);
        * project filter uses ``project_hash_ids`` (the project's ``hash_id``
          field), **not** UUID-form ``project_ids``;
        * ``grouping``, ``page``, ``per_page``, ``rounding`` are required.

        We accept ``project_ids`` (UUIDs) here for ergonomics and convert them
        to ``project_hash_ids`` by looking up :meth:`list_projects` (v2).
        """
        if not columns:
            raise ValueError("`columns` cannot be empty for report export.")

        from_dmy = _to_dmy(from_date)
        to_dmy = _to_dmy(to_date)

        # Convert UUIDs to hash_ids (the only form the API accepts on export).
        project_hash_ids: list[str] = []
        if project_ids:
            project_hash_ids = self._uuids_to_project_hash_ids(project_ids)

        filters: dict[str, Any] = {
            "from": from_dmy,
            "to": to_dmy,
            "timespan": timespan,
        }
        if project_hash_ids:
            filters["project_hash_ids"] = project_hash_ids
        if user_ids:
            filters["user_ids"] = list(user_ids)
        if client_ids:
            filters["client_ids"] = list(client_ids)
        if task_ids:
            filters["task_ids"] = list(task_ids)
        if tag_ids:
            filters["tag_ids"] = list(tag_ids)
        if extra_filters:
            for k, v in extra_filters.items():
                filters[k] = v

        body: dict[str, Any] = {
            "export": {"type": type, "columns": list(columns)},
            "filters": filters,
            # Duplicate the filter fields at top level — required by the API.
            "from": from_dmy,
            "to": to_dmy,
            "timespan": timespan,
            "grouping": {"primary": "no_group"},
            "page": 1,
            "per_page": 20,
            "rounding": {"type": "no_rounding", "precision": 5},
        }
        if project_hash_ids:
            body["project_hash_ids"] = project_hash_ids

        return self._download(
            "POST", "/reports/detailed/export", json=body, default_name=f"timenotes-report.{type}",
        )

    def _uuids_to_project_hash_ids(self, project_ids: list[str]) -> list[str]:
        """Resolve UUID project ids into hash_ids via the v2 listing.

        Pass-through for entries that already look like a hash_id (12 chars,
        non-UUID), so callers can mix the two if they want.
        """
        wanted = set(project_ids)
        v2 = self._request("GET", "/projects", params={"per_page": 1000}, base_url=V2_BASE_URL)
        out: list[str] = []
        seen: set[str] = set()
        for p in v2.get("projects", []) if isinstance(v2, Mapping) else []:
            if not isinstance(p, Mapping):
                continue
            uuid = p.get("id")
            hash_id = p.get("hash_id")
            if uuid in wanted and isinstance(hash_id, str) and hash_id not in seen:
                out.append(hash_id); seen.add(hash_id)
        # Pass-through anything that didn't match — likely already hash_ids.
        for pid in project_ids:
            if pid not in {p.get("id") for p in (v2 or {}).get("projects", []) if isinstance(p, Mapping)} and pid not in seen:
                out.append(pid); seen.add(pid)
        return out

    def _download(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        default_name: str = "download.bin",
    ) -> dict[str, Any]:
        """Internal: do a request, but treat the body as raw bytes (file)."""
        url = f"{V2_BASE_URL}{path}"
        headers = {**self.headers, "Accept": "*/*"}
        resp = self._http.request(
            method, url,
            headers=headers,
            params=params,
            json=json,
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise TimenotesError(resp.status_code, body)
        return {
            "content": resp.content,
            "content_type": resp.headers.get("content-type", ""),
            "filename": _extract_filename(resp.headers.get("content-disposition", ""), default_name),
            "size_bytes": len(resp.content),
        }

    # --- timesheets (v2) -------------------------------------------------

    def get_timesheet(self, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", "/timesheets", params=params or {}, base_url=V2_BASE_URL)

    def get_timesheet_cell(self, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", "/timesheets/cell", params=params or {}, base_url=V2_BASE_URL)

    # --- bulk time-log operations ---------------------------------------

    def bulk_modify_time_logs(self, body: Mapping[str, Any]) -> Any:
        return self._request("PATCH", "/bulks/time_logs/modify", json=dict(body))

    def bulk_remove_time_logs(self, body: Mapping[str, Any]) -> Any:
        return self._request("PATCH", "/bulks/time_logs/remove", json=dict(body))

    def bulk_copy_time_logs(self, body: Mapping[str, Any]) -> Any:
        return self._request("POST", "/bulks/time_logs/copy", json=dict(body))

    def bulk_update_rates(self, body: Mapping[str, Any]) -> Any:
        return self._request("PATCH", "/bulks/time_logs/update_rates", json=dict(body))

    def bulk_recalculate_rates(self, body: Mapping[str, Any]) -> Any:
        return self._request("PATCH", "/bulks/time_logs/recalculate_rates", json=dict(body))

    # --- aggregates derived from time logs -------------------------------

    def _all_time_logs(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        """Fetch every time log in a date range, paging through results."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                "/time_logs",
                params={
                    "from": from_date,
                    "to": to_date,
                    "per_page": 200,
                    "page": page,
                },
            )
            items = data.get("time_logs", []) if isinstance(data, Mapping) else []
            if not items:
                break
            out.extend(items)
            meta = data.get("meta", {}) if isinstance(data, Mapping) else {}
            pg = meta.get("pagination", {}) if isinstance(meta, Mapping) else {}
            current = pg.get("current_page", page)
            total_pages = pg.get("total_pages", current)
            if current >= total_pages:
                break
            page = current + 1
        return out

    def time_per_client(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        """Sum durations per client across the given date range."""
        return _aggregate(
            self._all_time_logs(from_date=from_date, to_date=to_date),
            key="client",
        )

    def time_per_project(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        return _aggregate(
            self._all_time_logs(from_date=from_date, to_date=to_date),
            key="project",
        )

    def time_per_task(
        self, *, from_date: str, to_date: str, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        logs = self._all_time_logs(from_date=from_date, to_date=to_date)
        if project_id:
            logs = [
                log
                for log in logs
                if isinstance(log.get("project"), Mapping)
                and log["project"].get("id") == project_id
            ]
        return _aggregate(logs, key="task")

    def time_per_day(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        logs = self._all_time_logs(from_date=from_date, to_date=to_date)
        buckets: dict[str, int] = {}
        for log in logs:
            day = (log.get("start_at") or "")[:10] or "unknown"
            buckets[day] = buckets.get(day, 0) + int(log.get("duration") or 0)
        return [
            {"date": day, "duration_minutes": mins, "duration_hours": round(mins / 60, 2)}
            for day, mins in sorted(buckets.items())
        ]


def _drop_none(m: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if v is not None}


def _to_dmy(date_str: str) -> str:
    """Convert ``YYYY-MM-DD`` into ``DD/MM/YYYY`` (the format the export uses).

    Pass-through if the string is already in DD/MM/YYYY form.
    """
    if not isinstance(date_str, str):
        return date_str
    if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
        y, m, d = date_str.split("-")
        return f"{d}/{m}/{y}"
    return date_str


def _extract_filename(content_disposition: str, default: str) -> str:
    """Pull a filename out of a Content-Disposition header (RFC 6266 lite)."""
    if not content_disposition:
        return default
    # RFC 5987 form: filename*=UTF-8''my%20file.csv
    import re
    m = re.search(r"filename\*=(?:[^']*'')?([^;]+)", content_disposition)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', content_disposition)
    if m:
        return m.group(1).strip()
    return default


def _aggregate(logs: list[dict[str, Any]], *, key: str) -> list[dict[str, Any]]:
    """Group time logs by ``log[key]`` (client / project / task) and sum durations.

    The Timenotes API stores ``duration`` in **minutes**, not seconds — easy
    to get wrong because the value is unitless on the wire.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for log in logs:
        ref = log.get(key) if isinstance(log, Mapping) else None
        if not isinstance(ref, Mapping):
            continue
        rid = ref.get("id") or "(none)"
        bucket = buckets.setdefault(
            rid,
            {"id": rid, "name": ref.get("name"), "duration_minutes": 0, "entries": 0},
        )
        bucket["duration_minutes"] += int(log.get("duration") or 0)
        bucket["entries"] += 1
    out = list(buckets.values())
    for b in out:
        b["duration_hours"] = round(b["duration_minutes"] / 60, 2)
    out.sort(key=lambda x: x["duration_minutes"], reverse=True)
    return out


def _extract_token(data: Any) -> str | None:
    if not isinstance(data, Mapping):
        return None
    for key in ("access_token", "accessToken", "token", "auth_token"):
        if isinstance(data.get(key), str):
            return data[key]
    for nested_key in ("session", "user", "data"):
        nested = data.get(nested_key)
        if isinstance(nested, Mapping):
            found = _extract_token(nested)
            if found:
                return found
    return None


def _first_account_id(accounts: Any) -> str | int | None:
    """Pull the first usable workspace id from a /users_accounts response.

    The API returns a list of *memberships* — each has its own ``id`` (the
    membership id) and a nested ``account`` object with the workspace ``id``.
    The ``AccountId`` header must carry the nested workspace id, not the
    membership id.
    """
    if isinstance(accounts, Mapping):
        for key in ("users_accounts", "accounts", "data"):
            inner = accounts.get(key)
            if isinstance(inner, list):
                accounts = inner
                break
    if not isinstance(accounts, list) or not accounts:
        return None
    first = accounts[0]
    if not isinstance(first, Mapping):
        return None
    account = first.get("account")
    if isinstance(account, Mapping):
        for key in ("id", "account_id"):
            val = account.get(key)
            if isinstance(val, (str, int)):
                return val
    for key in ("account_id", "accountId", "workspace_id"):
        val = first.get(key)
        if isinstance(val, (str, int)):
            return val
    return None
