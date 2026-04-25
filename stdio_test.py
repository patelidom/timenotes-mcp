"""Smoke-test the MCP server via real JSON-RPC over stdio.

Spawns ``python -m timenotes_mcp`` as a subprocess and exchanges three
messages with it:

  1. ``initialize``           — handshake
  2. ``tools/list``           — confirms tools are registered
  3. ``tools/call timenotes_list_projects`` — full round-trip through
     the MCP protocol against the real Timenotes API.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"


def _drain_stderr(proc: subprocess.Popen) -> None:
    """Print server stderr (HTTP logs etc.) so debugging is easy if it hangs."""
    assert proc.stderr is not None
    for line in proc.stderr:
        sys.stderr.write(f"  [server] {line}")


def _send(proc: subprocess.Popen, msg: dict) -> None:
    assert proc.stdin is not None
    payload = json.dumps(msg) + "\n"
    proc.stdin.write(payload)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout unexpectedly")
    return json.loads(line)


def main() -> None:
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "timenotes_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_DIR),
    )
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()

    try:
        # 1. initialize
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke-test", "version": "0.1"},
            },
        })
        init = _recv(proc)
        server_name = init.get("result", {}).get("serverInfo", {}).get("name")
        print(f"initialize -> server name = {server_name!r}")

        # initialized notification (no response expected)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 2. tools/list
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = _recv(proc)
        tools = listed.get("result", {}).get("tools", [])
        print(f"tools/list -> {len(tools)} tools registered")
        names = [t["name"] for t in tools]
        for name in names[:10]:
            print(f"   - {name}")
        if len(names) > 10:
            print(f"   ... and {len(names) - 10} more")

        # 3. real tool call
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "timenotes_list_projects", "arguments": {}},
        })
        called = _recv(proc)
        result = called.get("result", {})
        is_error = result.get("isError", False)
        # MCP returns content as a list of {type:"text", text:"..."} blocks.
        content = result.get("content", [])
        text_blob = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        try:
            parsed = json.loads(text_blob) if text_blob else {}
        except json.JSONDecodeError:
            parsed = {"raw": text_blob[:200]}
        if is_error:
            print(f"tools/call timenotes_list_projects -> ERROR: {parsed}")
            sys.exit(1)
        meta = parsed.get("meta", {}) if isinstance(parsed, dict) else {}
        total = meta.get("pagination", {}).get("total_count")
        returned = len(parsed.get("projects", []) if isinstance(parsed, dict) else [])
        print(f"tools/call timenotes_list_projects -> projects[{returned}] (total={total})")
        print("\nALL CHECKS PASSED")
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
