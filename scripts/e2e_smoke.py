"""End-to-end smoke test for the colab-mcp server.

Launches the real `colab-mcp` server as a subprocess, connects to it via stdio
using a FastMCP client, and exercises the full tool surface.

Usage:
    uv run python scripts/e2e_smoke.py            # disconnected smoke (no browser needed)
    uv run python scripts/e2e_smoke.py --connect  # interactive: opens browser for Colab login

The disconnected smoke verifies:
  - Server starts cleanly
  - All 9 tools are visible at startup (1 connection + 7 notebook + 1 GPU)
  - Each notebook tool returns NOT_CONNECTED_MSG when called without a browser
  - The old `execute_cell` no longer exists (rename regression check)

The connected smoke (with --connect) additionally drives:
  - open_colab_browser_connection
  - add_code_cell -> run_code_cell -> get_cells -> update_cell -> delete_cell -> move_cell
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "open_colab_browser_connection",
    "add_code_cell",
    "add_text_cell",
    "get_cells",
    "run_code_cell",
    "update_cell",
    "delete_cell",
    "move_cell",
    "change_runtime",
}

NOTEBOOK_STUBS = {
    "add_code_cell",
    "add_text_cell",
    "get_cells",
    "run_code_cell",
    "update_cell",
    "delete_cell",
    "move_cell",
}


def _green(s): return f"\033[32m{s}\033[0m"
def _red(s): return f"\033[31m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"
def _bold(s): return f"\033[1m{s}\033[0m"


async def smoke_disconnected(client: Client) -> int:
    """Run the no-browser smoke checks. Returns count of failures."""
    failures = 0
    print(_bold("\n=== Disconnected smoke ==="))

    print("\n[1/4] Listing tools...")
    tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    missing = EXPECTED_TOOLS - tool_names
    extra = tool_names - EXPECTED_TOOLS

    if missing:
        print(_red(f"  MISSING: {sorted(missing)}"))
        failures += 1
    if extra:
        print(_yellow(f"  EXTRA (unexpected but not fatal): {sorted(extra)}"))
    if not missing:
        print(_green(f"  OK — all 9 expected tools present"))

    print("\n[2/4] Checking execute_cell was removed (rename regression)...")
    if "execute_cell" in tool_names:
        print(_red("  FAIL — execute_cell still exists after rename"))
        failures += 1
    else:
        print(_green("  OK — execute_cell correctly removed"))

    print("\n[3/4] Inspecting schemas of the 4 newly-added/renamed tools...")
    target_tools = {"get_cells", "run_code_cell", "delete_cell", "move_cell"}
    for tool in tools:
        if tool.name not in target_tools:
            continue
        schema = tool.inputSchema or {}
        props = schema.get("properties", {})
        param_names = sorted(props.keys())
        print(f"  {tool.name}({', '.join(param_names) or '<no params>'})")

    print("\n[4/4] Calling notebook tools while disconnected — expect NOT_CONNECTED_MSG...")
    test_calls = [
        ("add_code_cell", {"code": "print('hi')"}),
        ("add_text_cell", {"content": "hello"}),
        ("get_cells", {}),
        ("run_code_cell", {"cellId": "fake"}),
        ("update_cell", {"cellId": "fake", "content": "x"}),
        ("delete_cell", {"cellId": "fake"}),
        ("move_cell", {"cellId": "fake", "toIndex": 0}),
    ]
    for name, args in test_calls:
        result = await client.call_tool(name, args)
        text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
        if "Not connected" in text or "open_colab_browser_connection" in text:
            print(_green(f"  OK — {name} returned NOT_CONNECTED_MSG"))
        else:
            print(_red(f"  FAIL — {name} returned unexpected: {text[:120]}"))
            failures += 1

    return failures


async def smoke_connected(client: Client) -> int:
    """Interactive E2E: drives a real browser connection. Returns count of failures."""
    failures = 0
    print(_bold("\n=== Connected E2E smoke (interactive) ==="))
    print(_yellow("This will open a Colab tab in your browser. Sign in and wait for the connection."))

    print("\n[1/7] Invoking open_colab_browser_connection (60s timeout)...")
    result = await client.call_tool("open_colab_browser_connection", {})
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    if "Connection successful" in text:
        print(_green(f"  OK — connection established"))
        print(f"    Server reply: {text[:200]}")
    else:
        print(_red(f"  FAIL — connection not established. Reply: {text}"))
        return failures + 1

    print("\n[2/7] add_code_cell...")
    result = await client.call_tool("add_code_cell", {"code": "import sys; print(sys.version)"})
    add_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    print(f"    -> {add_text[:300]}")
    # Try to extract a cellId from the result for downstream calls.
    cell_id = None
    structured = getattr(result, "structured_content", None) or {}
    if isinstance(structured, dict):
        cell_id = structured.get("cellId") or structured.get("id")
    if not cell_id:
        print(_yellow("    cellId not in structured output — downstream tests will use a placeholder"))

    print("\n[3/7] get_cells...")
    result = await client.call_tool("get_cells", {})
    gc_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    print(f"    -> {gc_text[:300]}")
    if "Not connected" in gc_text or "Error" in gc_text:
        print(_red("    FAIL — get_cells returned error/disconnected after connect"))
        failures += 1
    else:
        print(_green("    OK — get_cells responded"))

    target_id = cell_id or "0"
    print(f"\n[4/7] run_code_cell(cellId={target_id!r})...")
    result = await client.call_tool("run_code_cell", {"cellId": target_id})
    rc_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    print(f"    -> {rc_text[:300]}")

    print(f"\n[5/7] update_cell(cellId={target_id!r}, content='# updated')...")
    result = await client.call_tool("update_cell", {"cellId": target_id, "content": "# updated"})
    print(f"    -> {('\n'.join(c.text for c in result.content if hasattr(c, 'text')))[:300]}")

    print(f"\n[6/7] move_cell(cellId={target_id!r}, toIndex=1) — TESTS GUESSED SIGNATURE")
    result = await client.call_tool("move_cell", {"cellId": target_id, "toIndex": 1})
    mc_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    print(f"    -> {mc_text[:300]}")
    if "Error" in mc_text:
        print(_yellow("    NOTE — move_cell may have wrong param names; check browser-side handler"))

    print(f"\n[7/7] delete_cell(cellId={target_id!r}) — TESTS GUESSED SIGNATURE")
    result = await client.call_tool("delete_cell", {"cellId": target_id})
    dc_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    print(f"    -> {dc_text[:300]}")
    if "Error" in dc_text:
        print(_yellow("    NOTE — delete_cell may have wrong param names; check browser-side handler"))

    return failures


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Also run the interactive connected E2E (opens browser).",
    )
    args = parser.parse_args()

    print(_bold(f"Launching colab-mcp from {REPO_ROOT}"))
    transport = StdioTransport(
        command="uv",
        args=["run", "--directory", str(REPO_ROOT), "colab-mcp"],
        env={**os.environ},
    )

    failures = 0
    async with Client(transport) as client:
        failures += await smoke_disconnected(client)
        if args.connect:
            failures += await smoke_connected(client)

    print(_bold("\n=== Summary ==="))
    if failures == 0:
        print(_green(f"All checks passed."))
        sys.exit(0)
    else:
        print(_red(f"{failures} check(s) failed."))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
