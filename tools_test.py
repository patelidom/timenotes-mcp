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

    print("ALL TOOL-SHAPE CHECKS PASSED")


if __name__ == "__main__":
    main()
