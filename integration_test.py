"""End-to-end integration test for the Timenotes MCP client.

Safety contract:
  * READ operations may touch any data.
  * MUTATING operations are restricted to data this script creates itself.
  * IDs of created data are tracked in ``CREATED_IDS`` so the cleanup phase
    can never delete anything else, and an ``atexit`` hook guarantees cleanup
    even on early termination.
"""

from __future__ import annotations

import atexit
import datetime as dt
import os
import sys
from typing import Any

from timenotes_mcp._secrets import load_secrets
from timenotes_mcp.client import TimenotesClient, TimenotesError


CREATED_IDS: dict[str, list[Any]] = {
    "time_logs":   [],
    "projects":    [],
    "clients":     [],
    "tasks":       [],   # entries: (project_id, task_id)
    "tags":        [],
}
RESULTS: list[tuple[str, str, str]] = []
CLIENT: TimenotesClient | None = None


# -------------------------------------------------------------------------
# helpers
# -------------------------------------------------------------------------

def step(label: str, fn) -> Any:
    try:
        result = fn()
    except TimenotesError as exc:
        RESULTS.append((label, "ERR", f"{exc.status} {exc.body}"))
        print(f"  ERR  {label}: {exc.status} {exc.body}")
        return None
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((label, "FAIL", repr(exc)))
        print(f"  FAIL {label}: {exc!r}")
        return None
    summary = _summary(result)
    RESULTS.append((label, "OK", summary))
    print(f"  OK   {label}: {summary}")
    return result


def _summary(result: Any) -> str:
    if isinstance(result, list):
        return f"list[{len(result)}]"
    if isinstance(result, dict):
        for key in (
            "projects", "tasks", "tags", "time_logs", "users_accounts", "clients",
            "alerts", "invitations", "members_groups", "integrations",
            "absences", "absence_requests", "absence_types", "free_days",
            "plans", "subscription_periods", "accounts",
        ):
            if key in result and isinstance(result[key], list):
                return f"{key}[{len(result[key])}]"
        if not result:
            return "{}"
        return f"keys={list(result)[:5]}"
    if result is None:
        return "None"
    return type(result).__name__


def _final_cleanup() -> None:
    """atexit hook — delete any test resource still in CREATED_IDS."""
    if CLIENT is None:
        return
    for log_id in list(CREATED_IDS["time_logs"]):
        try:
            CLIENT.delete_time_log(log_id)
            CREATED_IDS["time_logs"].remove(log_id)
            print(f"  cleanup: deleted time_log {log_id[:8]}")
        except TimenotesError:
            pass
    for proj_id, task_id in list(CREATED_IDS["tasks"]):
        try:
            CLIENT.delete_task(proj_id, task_id)
            CREATED_IDS["tasks"].remove((proj_id, task_id))
            print(f"  cleanup: deleted task {task_id[:8]}")
        except TimenotesError:
            pass
    for proj_id in list(CREATED_IDS["projects"]):
        try:
            CLIENT.delete_project(proj_id)
            CREATED_IDS["projects"].remove(proj_id)
            print(f"  cleanup: deleted project {proj_id[:8]}")
        except TimenotesError:
            pass
    for cli_id in list(CREATED_IDS["clients"]):
        try:
            CLIENT.delete_client(cli_id)
            CREATED_IDS["clients"].remove(cli_id)
            print(f"  cleanup: deleted client {cli_id[:8]}")
        except TimenotesError:
            pass
    for tag_id in list(CREATED_IDS["tags"]):
        try:
            CLIENT.delete_tag(tag_id)
            CREATED_IDS["tags"].remove(tag_id)
            print(f"  cleanup: deleted tag {tag_id[:8]}")
        except TimenotesError:
            pass


# -------------------------------------------------------------------------
# test
# -------------------------------------------------------------------------

def run() -> None:
    global CLIENT
    load_secrets()
    CLIENT = TimenotesClient()
    CLIENT.login(os.environ["TIMENOTES_EMAIL"], os.environ["TIMENOTES_PASSWORD"])
    atexit.register(_final_cleanup)
    print(f"Logged in. Account: {CLIENT.account_id}")

    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    month_ago = today - dt.timedelta(days=30)
    fmt = lambda d: d.strftime("%Y-%m-%d")  # noqa: E731

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------
    print("\n[READ] account & lookups")
    step("whoami",                  lambda: CLIENT.current_user())
    step("current_account (v1)",    lambda: CLIENT.current_account())
    step("current_account_v2",      lambda: CLIENT.current_account_v2())
    step("list_accounts (v1)",      lambda: CLIENT.list_accounts())
    step("list_accounts_v2",        lambda: CLIENT.list_accounts_v2())
    step("list_owned_accounts",     lambda: CLIENT.list_owned_accounts())
    step("list_scoped_accounts",    lambda: CLIENT.list_scoped_accounts())

    projects = step("list_projects",   lambda: CLIENT.list_projects())
    aquashop = _project_named(projects, "aquashop")
    if aquashop:
        step("get_project[aquashop]",  lambda: CLIENT.get_project(aquashop["id"]))
        tasks = step(f"list_tasks[aquashop]",
                     lambda: CLIENT.list_tasks(aquashop["id"]))
        if tasks and tasks.get("tasks"):
            first_task = tasks["tasks"][0]
            step("get_task[aquashop, first]",
                 lambda: CLIENT.get_task(aquashop["id"], first_task["id"]))

    clients = step("list_clients (v2)",  lambda: CLIENT.list_clients())
    if isinstance(clients, dict) and clients.get("clients"):
        first_client = clients["clients"][0]
        step("get_client[first]",  lambda: CLIENT.get_client(first_client["id"]))

    step("list_members",            lambda: CLIENT.list_members())
    step("list_tags",               lambda: CLIENT.list_tags())

    print("\n[READ] dashboard / alerts / settings / plans / storage")
    step("get_dashboard",           lambda: CLIENT.get_dashboard())
    step("list_alerts",             lambda: CLIENT.list_alerts())
    step("get_setting",             lambda: CLIENT.get_setting())
    step("list_plans",              lambda: CLIENT.list_plans())
    step("current_subscription",    lambda: CLIENT.current_subscription_period())
    step("list_subscription_periods", lambda: CLIENT.list_subscription_periods())
    step("get_storage",             lambda: CLIENT.get_storage())

    print("\n[READ] holidays / absences")
    step("list_absence_types",      lambda: CLIENT.list_absence_types())
    step("list_free_days",          lambda: CLIENT.list_free_days())
    step("list_absence_requests (30d)",
         lambda: CLIENT.list_absence_requests({"from": fmt(month_ago), "to": fmt(today)}))
    step("list_absences (30d)",
         lambda: CLIENT.list_absences({"from": fmt(month_ago), "to": fmt(today)}))

    print("\n[READ] team / invitations / groups / integrations")
    step("list_invitations",            lambda: CLIENT.list_invitations())
    step("list_members_groups",         lambda: CLIENT.list_members_groups())
    step("list_integrations",           lambda: CLIENT.list_integrations())
    step("list_available_integrations", lambda: CLIENT.list_available_integrations())
    step("list_integration_accounts",   lambda: CLIENT.list_integration_accounts())

    print("\n[READ] tracker")
    pre_tracker = step("get_active_tracker", lambda: CLIENT.get_active_tracker())

    print("\n[READ] time_logs and aggregates")
    step("list_time_logs (7d)",
         lambda: CLIENT.list_time_logs(from_date=fmt(week_ago), to_date=fmt(today)))
    step("time_per_client (30d)",
         lambda: CLIENT.time_per_client(from_date=fmt(month_ago), to_date=fmt(today)))
    step("time_per_project (30d)",
         lambda: CLIENT.time_per_project(from_date=fmt(month_ago), to_date=fmt(today)))
    step("time_per_day (30d)",
         lambda: CLIENT.time_per_day(from_date=fmt(month_ago), to_date=fmt(today)))

    print("\n[READ] reports & timesheets")
    step("report_detailed (7d)",
         lambda: CLIENT.report_detailed({"from": fmt(week_ago), "to": fmt(today)}))
    step("report_chart (7d)",
         lambda: CLIENT.report_detailed_chart({"from": fmt(week_ago), "to": fmt(today)}))
    step("report_export_columns",  lambda: CLIENT.report_export_columns())
    step("get_timesheet (7d)",
         lambda: CLIENT.get_timesheet({"from": fmt(week_ago), "to": fmt(today)}))

    # ------------------------------------------------------------------
    # WRITE — only on test-created entities
    # ------------------------------------------------------------------
    print("\n[WRITE] client CRUD on test client")
    test_client = step("create_client",
                       lambda: CLIENT.create_client({"name": "MCP integration test client"}))
    test_client_id = (test_client or {}).get("client", {}).get("id") if isinstance(test_client, dict) else None
    if test_client_id:
        CREATED_IDS["clients"].append(test_client_id)
        step("update_client",
             lambda: CLIENT.update_client(test_client_id, {"name": "MCP integration test client (updated)"}))
        step("delete_client",
             lambda: (CLIENT.delete_client(test_client_id), CREATED_IDS["clients"].remove(test_client_id))[0])

    print("\n[WRITE] project CRUD on test project")
    test_project = step("create_project",
                        lambda: CLIENT.create_project({"name": "MCP integration test project"}))
    test_project_id = (test_project or {}).get("project", {}).get("id") if isinstance(test_project, dict) else None
    if test_project_id:
        CREATED_IDS["projects"].append(test_project_id)
        step("update_project",
             lambda: CLIENT.update_project(test_project_id, {"name": "MCP test project (updated)"}))

        print("\n[WRITE] task CRUD on test project")
        test_task = step("create_task",
                         lambda: CLIENT.create_task(test_project_id, {"name": "MCP test task"}))
        test_task_id = (test_task or {}).get("task", {}).get("id") if isinstance(test_task, dict) else None
        if test_task_id:
            CREATED_IDS["tasks"].append((test_project_id, test_task_id))
            step("update_task",
                 lambda: CLIENT.update_task(test_project_id, test_task_id, {"name": "MCP task (updated)"}))
            step("bookmark_task",
                 lambda: CLIENT.bookmark_task(test_project_id, test_task_id))
            step("unbookmark_task",
                 lambda: CLIENT.unbookmark_task(test_project_id, test_task_id))

            # Use this fresh task for time_log / tracker tests so we don't touch user data.
            print("\n[WRITE] time_log CRUD on test task")
            created = step("create_time_log",
                           lambda: CLIENT.create_time_log({
                               "project_id": test_project_id,
                               "task_id": test_task_id,
                               "date": fmt(today),
                               "start_at": "09:00",
                               "duration": 60,
                               "description": "MCP integration test log",
                           }))
            log_id = (created or {}).get("time_log", {}).get("id") if isinstance(created, dict) else None
            if log_id:
                CREATED_IDS["time_logs"].append(log_id)
                step("update_time_log",
                     lambda: CLIENT.update_time_log(log_id, {"duration": 120}))
                step("delete_time_log",
                     lambda: (CLIENT.delete_time_log(log_id), CREATED_IDS["time_logs"].remove(log_id))[0])

            print("\n[WRITE] tracker lifecycle on test task")
            if pre_tracker:
                print("  SKIP: a tracker is already running on this account.")
            else:
                step("start_tracker",
                     lambda: CLIENT.start_tracker({
                         "project_id": test_project_id,
                         "task_id": test_task_id,
                         "description": "MCP test tracker",
                     }))
                step("update_active_tracker",
                     lambda: CLIENT.update_active_tracker({"description": "MCP test tracker (edited)"}))
                step("stop_tracker", lambda: CLIENT.stop_tracker())
                # Stop converts tracker -> time log; clean up.
                try:
                    recent = CLIENT.list_time_logs(
                        from_date=fmt(today - dt.timedelta(days=1)),
                        to_date=fmt(today + dt.timedelta(days=1)),
                    ).get("time_logs", [])
                    for log in recent:
                        if "MCP test tracker" in (log.get("description") or ""):
                            CLIENT.delete_time_log(log["id"])
                except TimenotesError:
                    pass

            # Clean up test task.
            step("delete_task",
                 lambda: (CLIENT.delete_task(test_project_id, test_task_id),
                          CREATED_IDS["tasks"].remove((test_project_id, test_task_id)))[0])

        # Clean up test project.
        step("delete_project",
             lambda: (CLIENT.delete_project(test_project_id),
                      CREATED_IDS["projects"].remove(test_project_id))[0])

    print("\n[WRITE] tag CRUD")
    test_tag = step("create_tag", lambda: CLIENT.create_tag({"name": "mcp-test-tag"}))
    test_tag_id = (test_tag or {}).get("tag", {}).get("id") if isinstance(test_tag, dict) else None
    if test_tag_id:
        CREATED_IDS["tags"].append(test_tag_id)
        step("update_tag",
             lambda: CLIENT.update_tag(test_tag_id, {"name": "mcp-test-tag-updated"}))
        step("delete_tag",
             lambda: (CLIENT.delete_tag(test_tag_id), CREATED_IDS["tags"].remove(test_tag_id))[0])

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n========================  RESULTS  ========================")
    ok = sum(1 for _, s, _ in RESULTS if s == "OK")
    err = sum(1 for _, s, _ in RESULTS if s != "OK")
    print(f"OK={ok}  ERR/FAIL={err}")
    if err:
        print("\nFailures:")
        for label, status, detail in RESULTS:
            if status != "OK":
                print(f"  [{status}] {label}  --  {detail}")

    leftover = sum(len(v) for v in CREATED_IDS.values())
    if leftover:
        print(f"\nWARNING: {leftover} created entities still in CREATED_IDS at end of run: {CREATED_IDS}")
    sys.exit(0 if err == 0 else 1)


def _project_named(projects: Any, name: str) -> dict | None:
    if not isinstance(projects, dict):
        return None
    for p in projects.get("projects", []):
        if isinstance(p, dict) and p.get("name") == name:
            return p
    return None


if __name__ == "__main__":
    run()
