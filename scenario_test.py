"""Real-life scenario test: tools working TOGETHER through the real MCP protocol.

Spins up a fake Timenotes API (which also validates auth headers and request
body shapes), points the MCP server at it via TIMENOTES_BASE_URL, then drives
a realistic workday over JSON-RPC:

  login -> whoami -> list_projects -> list_tasks -> create 3 time logs
  -> list_time_logs -> time_per_project (paginated aggregation)
  -> tracker start/get/stop -> update_project(fields) -> delete_time_log

No real credentials needed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TOKEN = "tok-123"
ACCOUNT = "acc-1"

state = {
    "projects": [
        {"id": "p-1", "hash_id": "hp1", "name": "Web", "archived": False},
        {"id": "p-2", "hash_id": "hp2", "name": "Mobile", "archived": False},
    ],
    "tasks": {"p-1": [{"id": "t-1", "name": "Frontend"}, {"id": "t-2", "name": "Backend"}],
              "p-2": [{"id": "t-3", "name": "iOS"}]},
    "time_logs": [],
    "tracker": None,
    "log_seq": 0,
    "header_violations": [],
}


def _check_auth(req: Request, *, account_required: bool = True) -> Response | None:
    if req.headers.get("AuthorizationToken") != TOKEN:
        return JSONResponse({"error": "bad token"}, status_code=401)
    if account_required and req.headers.get("AccountId") != ACCOUNT:
        state["header_violations"].append(f"{req.method} {req.url.path}: AccountId={req.headers.get('AccountId')!r}")
        return JSONResponse({"error": "missing AccountId"}, status_code=401)
    return None


async def sessions(req: Request):
    body = await req.json()
    if body.get("password") != "correct-horse":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"token": TOKEN, "user": {"id": "u-1", "email": body["email"]}})


async def users_accounts(req: Request):
    if err := _check_auth(req, account_required=False):
        return err
    # The client must send this one WITHOUT AccountId (chicken-and-egg).
    if req.headers.get("AccountId"):
        state["header_violations"].append("users_accounts listed WITH AccountId header")
    return JSONResponse({"users_accounts": [
        {"id": "m-1", "account": {"id": ACCOUNT, "name": "Test Workspace"}},
    ]})


async def users_accounts_current(req: Request):
    if err := _check_auth(req):
        return err
    return JSONResponse({"users_account": {"id": "m-1", "account": {"id": ACCOUNT, "name": "Test Workspace"}}})


async def projects(req: Request):
    if err := _check_auth(req):
        return err
    return JSONResponse({"projects": state["projects"]})


async def project_tasks(req: Request):
    if err := _check_auth(req):
        return err
    return JSONResponse({"tasks": state["tasks"].get(req.path_params["pid"], [])})


async def patch_project(req: Request):
    if err := _check_auth(req):
        return err
    body = await req.json()
    # Regression for the **fields bug: body must be {"project": {<flat fields>}}.
    proj_fields = body.get("project")
    assert isinstance(proj_fields, dict) and "fields" not in proj_fields, f"bad PATCH body: {body}"
    for p in state["projects"]:
        if p["id"] == req.path_params["pid"]:
            p.update(proj_fields)
            return JSONResponse({"project": p})
    return JSONResponse({"error": "not found"}, status_code=404)


async def time_logs(req: Request):
    if err := _check_auth(req):
        return err
    if req.method == "POST":
        body = (await req.json())["time_log"]
        state["log_seq"] += 1
        project = next(p for p in state["projects"] if p["id"] == body["project_id"])
        task = next(t for t in state["tasks"][body["project_id"]] if t["id"] == body["task_id"])
        log = {"id": f"log-{state['log_seq']}", "duration": body["duration"],
               "start_at": f"{body['date']}T{body['start_at']}:00Z",
               "description": body.get("description"),
               "project": {"id": project["id"], "name": project["name"]},
               "task": {"id": task["id"], "name": task["name"]},
               "client": {"id": "c-1", "name": "Acme"}}
        state["time_logs"].append(log)
        return JSONResponse({"time_log": log})
    # GET. The real API rejects single-day ranges with an empty 422 — mirror
    # that so the client-side widening workaround stays covered.
    if req.query_params.get("from") and req.query_params.get("from") == req.query_params.get("to"):
        return Response(status_code=422)
    # Paginate with a small server-side cap to exercise the paging loop.
    page = int(req.query_params.get("page", 1))
    per_page = min(int(req.query_params.get("per_page", 100)), 2)
    logs = state["time_logs"]
    total_pages = max(1, -(-len(logs) // per_page))
    chunk = logs[(page - 1) * per_page: page * per_page]
    return JSONResponse({"time_logs": chunk,
                         "meta": {"pagination": {"current_page": page, "total_pages": total_pages}}})


async def time_log_delete(req: Request):
    if err := _check_auth(req):
        return err
    state["time_logs"] = [l for l in state["time_logs"] if l["id"] != req.path_params["lid"]]
    return Response(status_code=204)


async def tracker(req: Request):
    if err := _check_auth(req):
        return err
    if req.method == "POST":
        body = (await req.json())["active_tracker"]
        state["tracker"] = {"id": "tr-1", **body}
        return JSONResponse({"active_tracker": state["tracker"]})
    if req.method == "DELETE":
        state["tracker"] = None
        return Response(status_code=204)
    if state["tracker"] is None:
        return JSONResponse({"error": "no tracker"}, status_code=404)
    return JSONResponse({"active_tracker": state["tracker"]})


app = Starlette(routes=[
    Route("/v1/sessions", sessions, methods=["POST"]),
    Route("/v1/users_accounts", users_accounts, methods=["GET"]),
    Route("/v1/users_accounts/current", users_accounts_current, methods=["GET"]),
    Route("/v1/projects", projects, methods=["GET"]),
    Route("/v2/projects/{pid}/tasks", project_tasks, methods=["GET"]),
    Route("/v2/projects/{pid}", patch_project, methods=["PATCH"]),
    Route("/v1/time_logs", time_logs, methods=["GET", "POST"]),
    Route("/v1/time_logs/{lid}", time_log_delete, methods=["DELETE"]),
    Route("/v1/active_tracker", tracker, methods=["GET"]),
    Route("/v2/active_tracker", tracker, methods=["POST", "DELETE"]),
])


# --- MCP JSON-RPC plumbing ---------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Mcp:
    def __init__(self, base_url: str):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "timenotes_mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env={**os.environ, "TIMENOTES_BASE_URL": base_url},
        )
        self.seq = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "scenario", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method, params):
        self.seq += 1
        self._send({"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params})
        resp = json.loads(self.proc.stdout.readline())
        assert "error" not in resp, resp
        return resp["result"]

    def call(self, tool, args=None):
        res = self._rpc("tools/call", {"name": tool, "arguments": args or {}})
        text = res["content"][0]["text"] if res.get("content") else "{}"
        assert not res.get("isError"), f"{tool} failed: {text}"
        return json.loads(text)

    def close(self):
        self.proc.terminate()


def main() -> None:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    mcp = Mcp(f"http://127.0.0.1:{port}/v1")
    try:
        # 1. Login picks the workspace automatically.
        r = mcp.call("timenotes_login", {"email": "Test@Example.com", "password": "correct-horse"})
        assert r["ok"] and r["account_id"] == ACCOUNT, r
        print(f"[login] ok, account={r['account_id']}")

        # 2. whoami reflects the cached user + selected account.
        r = mcp.call("timenotes_whoami")
        assert r["user"]["email"] == "test@example.com", r
        assert r["account"]["users_account"]["account"]["id"] == ACCOUNT, r
        print(f"[whoami] {r['user']['email']} @ {r['account']['users_account']['account']['name']}")

        # 3. Discover project -> task (IDs flow between tools).
        projects = mcp.call("timenotes_list_projects")["projects"]
        web = next(p for p in projects if p["name"] == "Web")
        tasks = mcp.call("timenotes_list_tasks", {"project_id": web["id"]})["tasks"]
        frontend = next(t for t in tasks if t["name"] == "Frontend")
        print(f"[discover] project={web['id']} task={frontend['id']}")

        # 4. Log a workday: 90 + 60 min on Web, 30 min on Mobile.
        log1 = mcp.call("timenotes_create_time_log", {
            "project_id": "p-1", "task_id": "t-1", "date": "2026-07-10",
            "start_at": "09:00", "duration": 90, "description": "review PR"})["time_log"]
        mcp.call("timenotes_create_time_log", {
            "project_id": "p-1", "task_id": "t-2", "date": "2026-07-10",
            "start_at": "11:00", "duration": 60})
        log3 = mcp.call("timenotes_create_time_log", {
            "project_id": "p-2", "task_id": "t-3", "date": "2026-07-10",
            "start_at": "14:00", "duration": 30})["time_log"]
        print(f"[create] 3 logs, first={log1['id']}")

        # 5. Aggregation pages through the API (fake caps per_page at 2 -> 2 pages).
        agg = mcp.call("timenotes_time_per_project",
                       {"from_date": "2026-07-10", "to_date": "2026-07-10"})
        assert agg["count"] == 2, agg
        assert agg["projects"][0] == {"id": "p-1", "name": "Web", "duration_minutes": 150,
                                      "entries": 2, "duration_hours": 2.5}, agg
        assert agg["projects"][1]["duration_minutes"] == 30, agg
        print(f"[aggregate] Web={agg['projects'][0]['duration_hours']}h Mobile=0.5h (paginated)")

        # 6. Tracker lifecycle.
        mcp.call("timenotes_start_tracker", {"project_id": "p-1", "description": "standup"})
        running = mcp.call("timenotes_get_active_tracker")
        assert running["active_tracker"]["description"] == "standup", running
        mcp.call("timenotes_stop_tracker")
        assert mcp.call("timenotes_get_active_tracker") == {}, "tracker should be stopped"
        print("[tracker] start -> get -> stop ok")

        # 7. Update flows back into subsequent reads (and body shape is flat).
        mcp.call("timenotes_update_project", {"project_id": "p-1", "fields": {"name": "Web v2"}})
        names = [p["name"] for p in mcp.call("timenotes_list_projects")["projects"]]
        assert "Web v2" in names, names
        print("[update] project rename visible in list_projects")

        # 8. Delete shrinks the list.
        mcp.call("timenotes_delete_time_log", {"time_log_id": log3["id"]})
        left = mcp.call("timenotes_list_time_logs",
                        {"from_date": "2026-07-10", "to_date": "2026-07-10"})["time_logs"]
        assert len(left) == 2, left
        print("[delete] time log removed")

        # 9. Wrong password surfaces as a tool error, not a crash.
        try:
            mcp.call("timenotes_login", {"email": "x@x.cz", "password": "wrong"})
            raise AssertionError("login with wrong password should fail")
        except AssertionError as exc:
            if "should fail" in str(exc):
                raise
        print("[auth-error] wrong password rejected cleanly")

        assert not state["header_violations"], state["header_violations"]
        print("\nALL SCENARIO CHECKS PASSED")
    finally:
        mcp.close()
        server.should_exit = True


if __name__ == "__main__":
    main()
