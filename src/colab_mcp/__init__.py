# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import asyncio
import datetime
import logging
import os
import tempfile
import sys
import webbrowser

from fastmcp import FastMCP
from fastmcp.utilities import logging as fastmcp_logger

from colab_mcp.session import ColabSessionProxy, NOT_CONNECTED_MSG
from colab_mcp.websocket_server import COLAB, SCRATCH_PATH
from colab_mcp import process_registry


mcp = FastMCP(name="ColabMCP")

# These will be set during main_async() startup
_proxy_client = None
_session_mcp = None
_colab_client = None  # For runtime API (assign/unassign GPU)


async def _forward_or_stub(tool_name: str, arguments: dict) -> str:
    """Forward a tool call to the browser if connected, otherwise return stub message."""
    if _proxy_client is not None and _proxy_client.is_connected():
        try:
            result = await _proxy_client.proxy_mcp_client.call_tool(tool_name, arguments)
            # Extract text from result
            if hasattr(result, 'content'):
                return "\n".join(c.text for c in result.content if hasattr(c, 'text'))
            return str(result)
        except Exception as e:
            return f"Error calling {tool_name}: {e}. Try calling open_colab_browser_connection to reconnect."
    return NOT_CONNECTED_MSG


@mcp.tool()
async def open_colab_browser_connection() -> str:
    """Opens a connection to a Google Colab browser session and unlocks notebook editing tools. Returns whether the connection attempt succeeded."""
    if _proxy_client is not None and _proxy_client.is_connected():
        return "Already connected to Colab."

    if _proxy_client is None:
        return "Server not initialized. Please wait and try again."

    webbrowser.open_new(
        f"{COLAB}{SCRATCH_PATH}#mcpProxyToken={_proxy_client.wss.token}&mcpProxyPort={_proxy_client.wss.port}"
    )

    # Wait for browser to connect
    await _proxy_client.await_proxy_connection()

    if _proxy_client.is_connected():
        tool_names = await _proxy_client.await_tools_ready()
        tools_text = ", ".join(tool_names) if tool_names else "none discovered"
        return f"Connection successful. Available notebook tools: {tools_text}. You can now create, edit, and execute cells in the Colab notebook."

    # Timed out — surface diagnostic info about other running servers so the
    # user can recognize the "old browser tab pointed at a dead port" case.
    try:
        others = [
            e for e in process_registry.list_running()
            if e.pid != os.getpid()
        ]
    except Exception:
        others = []
    my_port = _proxy_client.wss.port
    if others:
        peer_ports = ", ".join(f"{e.port} (pid {e.pid})" for e in others)
        return (
            f"Connection timed out. This server is on port {my_port}, but "
            f"{len(others)} other colab-mcp server(s) are also running: "
            f"{peer_ports}. If you have an old Colab tab open, it may be "
            "pointing at one of those instead of this server. Either close "
            "the old tab and let me open a fresh one, or run `colab-mcp "
            "--kill-stale` to clean up orphaned servers."
        )
    return (
        f"Connection timed out. This server is on port {my_port}. Please "
        "make sure you have a Colab notebook open in your browser and try "
        "again. If a Colab tab opened but says 'Disconnected from the local "
        "Colab MCP server', refresh that tab — the URL fragment contains the "
        "correct token+port for this server instance."
    )


@mcp.tool()
async def add_code_cell(code: str = "", cellIndex: int = 0, language: str = "python") -> str:
    """Add a new code cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("add_code_cell", {"code": code, "cellIndex": cellIndex, "language": language})


@mcp.tool()
async def add_text_cell(content: str = "", cellIndex: int = -1) -> str:
    """Add a new text/markdown cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("add_text_cell", {"content": content, "cellIndex": cellIndex})


@mcp.tool()
async def execute_cell(cellId: str = "", cellIndex: int = 0) -> str:
    """Execute a cell in the Colab notebook. Pass cellId (from add_code_cell result) or cellIndex. Requires an active browser connection via open_colab_browser_connection."""
    args = {}
    if cellId:
        args["cellId"] = cellId
    else:
        args["cellId"] = str(cellIndex)
    return await _forward_or_stub("run_code_cell", args)


@mcp.tool()
async def update_cell(cellId: str = "", content: str = "") -> str:
    """Update the contents of an existing cell in the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("update_cell", {"cellId": cellId, "content": content})


@mcp.tool()
async def change_runtime(accelerator: str = "T4") -> str:
    """Change the Colab runtime to use a specific GPU accelerator. Valid values: NONE, T4, L4, A100. Requires OAuth setup (first time opens browser for consent)."""
    if _colab_client is None:
        return "Runtime API not initialized. Start with --client-oauth-config flag pointing to your OAuth client secrets JSON."
    try:
        from colab_mcp.client import Accelerator, Variant
        import uuid

        acc = Accelerator(accelerator)
        variant = Variant.GPU if acc != Accelerator.NONE else Variant.DEFAULT
        notebook_hash = str(uuid.uuid4())

        # Unassign current VM if any
        try:
            assignments = _colab_client.list_assignments()
            for a in assignments:
                _colab_client.unassign(a.endpoint)
        except Exception:
            pass

        # Assign new VM
        result = _colab_client.assign(notebook_hash, variant, acc)
        return f"Runtime changed to {accelerator}. Endpoint: {result.endpoint}. Use open_colab_browser_connection to connect to the new runtime."
    except Exception as e:
        return f"Failed to change runtime: {e}"


def init_logger(logdir):
    log_filename = datetime.datetime.now().strftime(
        f"{logdir}/colab-mcp.%Y-%m-%d_%H-%M-%S.log"
    )
    logging.basicConfig(
        format="%(asctime)s %(levelname)s:%(message)s",
        datefmt="%m/%d/%Y %I:%M:%S %p",
        filename=log_filename,
        level=logging.INFO,
    )
    fastmcp_logger.get_logger("colab-mcp").info("logging to %s" % log_filename)


def parse_args(v):
    parser = argparse.ArgumentParser(
        description="ColabMCP is an MCP server that lets you interact with Colab."
    )
    parser.add_argument(
        "-l",
        "--log",
        help="if set, use this directory as a location for logfiles (if unset, will log to %s/colab-mcp-logs/)"
        % tempfile.gettempdir(),
        action="store",
        default=tempfile.mkdtemp(prefix="colab-mcp-logs-"),
    )
    parser.add_argument(
        "-p",
        "--enable-proxy",
        help="if set, enable the runtime proxy (enabled by default).",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--client-oauth-config",
        help="Path to OAuth client secrets JSON for Colab API access (enables change_runtime tool).",
        action="store",
        default=None,
    )
    parser.add_argument(
        "--list-running",
        help="List all currently-running colab-mcp servers and exit.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--kill-stale",
        help="Terminate all running colab-mcp servers (including this one is NOT included) and exit. Useful when the browser shows 'Disconnected from the local Colab MCP server' due to orphaned processes from prior sessions.",
        action="store_true",
        default=False,
    )
    return parser.parse_args(v)


def _print_running_servers() -> None:
    entries = process_registry.list_running()
    if not entries:
        print("No colab-mcp servers currently registered as running.")
        return
    print(f"Found {len(entries)} running colab-mcp server(s):")
    import datetime as _dt
    for e in entries:
        started = _dt.datetime.fromtimestamp(e.started_at).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  pid={e.pid:<6}  port={e.port:<6}  host={e.host}  started={started}")


async def main_async():
    global _proxy_client, _session_mcp, _colab_client
    args = parse_args(sys.argv[1:])
    init_logger(args.log)

    # Diagnostic / cleanup flags exit early.
    if args.list_running:
        _print_running_servers()
        return
    if args.kill_stale:
        removed = process_registry.cleanup_stale(kill=True)
        if not removed:
            print("No stale colab-mcp servers found.")
        else:
            print(f"Terminated {len(removed)} stale colab-mcp server(s):")
            for e in removed:
                print(f"  pid={e.pid} port={e.port}")
        return

    # Prune any dead entries from prior crashed runs BEFORE we bind a port.
    # This keeps the registry honest. We don't auto-kill ALIVE entries here —
    # multiple clients (e.g., two Claude Code instances) are valid; only the
    # browser-tab confusion is the bug, and the per-tab token fragment scopes
    # which server a tab talks to.
    dead = process_registry.prune_dead()
    if dead:
        logging.info(f"Pruned {dead} stale entries from process registry")

    if args.enable_proxy:
        logging.info("enabling session proxy tools")
        _session_mcp = ColabSessionProxy()
        await _session_mcp.start_proxy_server()
        _proxy_client = _session_mcp.proxy_client
        # Register ourselves now that we know the port.
        try:
            entry = process_registry.register(
                port=_session_mcp.wss.port,
                host=_session_mcp.wss.host,
            )
            logging.info(
                f"Registered colab-mcp pid={entry.pid} port={entry.port}"
            )
        except Exception as exc:
            logging.warning(f"Could not register process: {exc}")

    if args.client_oauth_config:
        try:
            from colab_mcp.auth import get_credentials
            from colab_mcp.client import ColabClient, Prod
            logging.info("initializing Colab API client with OAuth")
            session = get_credentials(args.client_oauth_config)
            _colab_client = ColabClient(Prod(), session)
            logging.info("Colab API client ready")
        except Exception as e:
            logging.warning(f"Failed to initialize Colab API client: {e}")

    try:
        await mcp.run_async()

    finally:
        if args.enable_proxy and _session_mcp:
            await _session_mcp.cleanup()
        # Always unregister so a clean shutdown doesn't leave a stale entry.
        try:
            process_registry.unregister()
        except Exception as exc:
            logging.warning(f"Could not unregister process: {exc}")


def main() -> None:
    asyncio.run(main_async())
