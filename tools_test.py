"""Offline check of MCP tool schemas and request-body shapes. No credentials needed.

Regression for the ``**fields`` bug: FastMCP turned ``**fields: Any`` into a
single required parameter literally named ``fields``, so update tools sent
``{"client": {"fields": {...}}}`` (double-wrapped) to the API and every
create tool required a ``fields`` argument.
"""

from __future__ import annotations

import asyncio

from timenotes_mcp import server


def main() -> None:
    server._client.access_token = "fake-token-for-offline-test"

    # 1. No tool exposes an untyped required kwargs-bag.
    for tool in server.mcp._tool_manager.list_tools():
        props = tool.parameters.get("properties", {})
        for name, schema in props.items():
            assert schema.get("type") or schema.get("anyOf"), (
                f"{tool.name}.{name} has no type in its JSON schema"
            )

    # 2. Update tool sends the fields dict as the PATCH body, unwrapped.
    captured: dict = {}
    server._client.update_client = (
        lambda cid, body: captured.update(cid=cid, body=dict(body)) or {"ok": 1}
    )
    asyncio.run(server.mcp._tool_manager.call_tool(
        "timenotes_update_client",
        {"client_id": "c-1", "fields": {"name": "Acme"}},
    ))
    assert captured["body"] == {"name": "Acme"}, captured

    # 3. Create tool works WITHOUT fields and merges them flat when given.
    server._client.create_project = (
        lambda body: captured.update(body=dict(body)) or {"ok": 1}
    )
    asyncio.run(server.mcp._tool_manager.call_tool(
        "timenotes_create_project", {"name": "P1"},
    ))
    assert captured["body"] == {"name": "P1"}, captured
    asyncio.run(server.mcp._tool_manager.call_tool(
        "timenotes_create_project", {"name": "P2", "fields": {"color": "#f00"}},
    ))
    assert captured["body"] == {"name": "P2", "color": "#f00"}, captured

    # 4. Custom base URL derivation without fallback to production.
    from timenotes_mcp.client import TimenotesClient
    c1 = TimenotesClient(base_url="https://timenotes-mcp.dpstudio.cz/api/v1")
    assert c1.v2_base_url == "https://timenotes-mcp.dpstudio.cz/api/v2", c1.v2_base_url
    c2 = TimenotesClient(base_url="https://timenotes-mcp.dpstudio.cz/api")
    assert c2.v2_base_url == "https://timenotes-mcp.dpstudio.cz/api/v2", c2.v2_base_url

    # 5. _compact_task does not discard 0 or 0.0 values.
    from timenotes_mcp.server import _compact_task
    t = {
        "id": "t-1",
        "name": "Task 1",
        "state": "active",
        "worktime": 0,
        "billable_rate": 0.0,
        "description": "",
        "tags": [],
        "is_billable": False,
    }
    compacted = _compact_task(t)
    assert compacted["worktime"] == 0, compacted
    assert compacted["billable_rate"] == 0.0, compacted
    assert "description" not in compacted, compacted
    assert "tags" not in compacted, compacted
    assert "is_billable" not in compacted, compacted

    # 6. timenotes_list_tasks raises ValueError on negative limit.
    from timenotes_mcp.server import timenotes_list_tasks
    try:
        timenotes_list_tasks(project_id="p-1", limit=-1)
        assert False, "Should raise ValueError for negative limit"
    except ValueError:
        pass

    print("ALL TOOL-SHAPE AND UNIT CHECKS PASSED")


if __name__ == "__main__":
    main()
