"""Offline checks of tool schemas and request shapes. No network, no token needed.

Run: python tools_test.py
"""

from __future__ import annotations

import asyncio

from timenotes_mcp import server
from mcp.server.mcpserver.exceptions import ToolError

from timenotes_mcp.client import flatten_params


def main() -> None:
    # 1. Rails-style query flattening (deepObject filters, arrays, bools, None).
    assert flatten_params({
        "filters": {"from": "2026-10-01", "to": None, "project_hash_ids": ["a", "b"], "is_billable": False},
        "per_page": 200,
    }) == [
        ("filters[from]", "2026-10-01"),
        ("filters[project_hash_ids][]", "a"), ("filters[project_hash_ids][]", "b"),
        ("filters[is_billable]", "false"),
        ("per_page", "200"),
    ]

    # 2. Every tool parameter is typed (FastMCP **kwargs regression).
    tools = asyncio.run(server.mcp.list_tools())
    for tool in tools:
        for name, schema in tool.input_schema.get("properties", {}).items():
            assert schema.get("type") or schema.get("anyOf"), f"{tool.name}.{name} untyped"
    names = {t.name for t in tools}
    assert {"timenotes_log_time", "timenotes_create_time_log", "timenotes_update_time_log",
            "timenotes_list_time_logs", "timenotes_list_tasks", "timenotes_time_per_task",
            "timenotes_api"} <= names, names

    # Fake API: records calls, serves canned lists.
    calls: list[tuple] = []
    lists = {
        "/projects": [{"id": "p-9", "hash_id": "h9", "name": "Acme"},
                      {"id": "p-10", "hash_id": "h10", "name": "Acme Internal"}],
        "/projects/p-9/tasks": [{"id": "t-a", "name": "standup"}],
        "/reports/detailed": [{"id": "t-a", "name": "standup", "duration": 90,
                               "project": {"name": "Acme"}},
                              {"id": "t-b", "name": "review", "duration": 30}],
        "/time_logs": [{"start_at": "2026-10-30T23:30:00Z", "duration": 45},
                       {"start_at": "2026-10-30T08:00:00Z", "duration": 15},
                       {"start_at": "2026-10-31T09:00:00Z", "duration": 60}],
    }

    def fake_request(method, path, *, params=None, json=None):
        calls.append((method, path, params, json))
        if method == "POST" and path.endswith("/tasks"):
            return {"task": {"id": "t-new", "name": json["task"]["name"]}}
        if method == "POST" and path == "/time_logs":
            return {"time_log": {"id": "tl-1", **json["time_log"]}}
        return None

    def fake_list_all(path, key, params=None):
        calls.append(("LIST", path, params, None))
        return lists.get(path, [])

    server._client.api_token = "fake"
    server._client.request = fake_request
    server._client.list_all = fake_list_all

    # 3. log_time: exact project match beats the substring cousin; task resolved.
    out = server.timenotes_log_time(project="acme", task="standup", duration=30)
    assert out["project"]["id"] == "p-9" and out["task"]["id"] == "t-a", out
    assert out["created_task"] is False
    body = calls[-1][3]["time_log"]
    assert body["duration"] == 30 and len(body["date"]) == 10 and len(body["start_at"]) == 5, body

    # 4. Ambiguous project fails loudly instead of guessing.
    try:
        server.timenotes_log_time(project="acm", task="x", duration=5)
        raise AssertionError("ambiguous project should raise")
    except ToolError as e:
        assert "ambiguous" in str(e).lower(), e

    # 5. Missing task gets created.
    out = server.timenotes_log_time(project="Acme", task="brand new", duration=15)
    assert out["task"]["id"] == "t-new" and out["created_task"] is True, out

    # 6. Report filters convert project UUIDs to hash_ids; grouping is passed.
    calls.clear()
    out = server.timenotes_time_per_task("2026-10-01", "2026-10-31", project_id="p-9")
    report = [c for c in calls if c[1] == "/reports/detailed"][0][2]
    assert report["filters"]["project_hash_ids"] == ["h9"], report
    assert report["grouping"] == {"primary": "task"}, report
    assert [r["duration_minutes"] for r in out["tasks"]] == [90, 30]
    assert out["tasks"][0]["project"] == "Acme" and out["tasks"][0]["duration_hours"] == 1.5

    # 7. Per-day buckets use the local date in start_at (no tz shift).
    days = server.timenotes_time_per_day("2026-10-30", "2026-10-31")["days"]
    assert days == [{"date": "2026-10-30", "duration_minutes": 60, "duration_hours": 1.0},
                    {"date": "2026-10-31", "duration_minutes": 60, "duration_hours": 1.0}], days

    # 8. Generic tool keeps the token on the API host.
    for bad in ("@evil.com/x", "https://evil.com/x", "clients"):
        try:
            server.timenotes_api("GET", bad)
            raise AssertionError(f"path {bad!r} should be rejected")
        except ToolError:
            pass
    server.timenotes_api("get", "/v2/clients", query={"per_page": 5})
    assert calls[-1][:2] == ("GET", "/clients"), calls[-1]

    # 9. Tools run through the MCP layer too (argument validation + return).
    res = asyncio.run(server.mcp.call_tool(
        "timenotes_update_time_log", {"time_log_id": "tl-1", "duration": 45}))
    assert not res.is_error, res
    assert calls[-1][:2] == ("PATCH", "/time_logs/tl-1") and calls[-1][3] == {"time_log": {"duration": 45}}

    # 10. API errors reach the model with their message (mcp 2.x hides plain exceptions).
    from timenotes_mcp.client import TimenotesError

    def failing(*a, **k):
        raise TimenotesError(422, {"errors": {"task": ["is invalid"]}})
    server._client.request = failing
    try:
        asyncio.run(server.mcp.call_tool("timenotes_delete_time_log", {"time_log_id": "x"}))
        raise AssertionError("API error should surface")
    except ToolError as e:  # the protocol layer turns this into an is_error result
        assert "is invalid" in str(e), e

    # 11. Single-owner lock: once bound, only the same Timenotes user may authorize.
    import tempfile
    from pathlib import Path

    from cryptography.fernet import Fernet

    from timenotes_mcp import http_app
    from timenotes_mcp.oauth import OAuthStore

    st = OAuthStore(Path(tempfile.mkdtemp()) / "o.db", Fernet.generate_key())
    assert http_app._owner_error(st, "anyone@x.cz") is None  # unbound: first login claims it
    st.save_timenotes_session(token="t", account_id=None, email="owner@x.cz")
    assert http_app._owner_error(st, "OWNER@x.cz") is None
    assert "different" in http_app._owner_error(st, "evil@y.cz")
    st.save_timenotes_session(token="t", account_id=None, email=None)  # env-seeded row
    http_app._token_email = lambda tok: "owner@x.cz"
    assert "different" in http_app._owner_error(st, "evil@y.cz")
    http_app._token_email = lambda tok: None  # owner unknown -> fail closed
    assert "verify" in http_app._owner_error(st, "owner@x.cz")

    print(f"ALL OFFLINE CHECKS PASSED ({len(names)} tools)")


if __name__ == "__main__":
    main()
