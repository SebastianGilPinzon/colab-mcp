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

# Runtime Mode state. When change_runtime() assigns a GPU VM, it also opens a
# direct Jupyter-kernel connection to that VM through the Colab runtime proxy.
# Cells then execute on the GPU VM (browserless) instead of the browser tab's
# default CPU kernel. _runtime_nbh is a *stable* notebook hash for this server
# process so repeated change_runtime calls reuse/replace the same runtime
# rather than orphaning a fresh VM each time (the original bug).
_runtime_kernel = None        # jupyter_kernel_client.KernelClient | None
_runtime_endpoint = None      # str | None — assigned VM endpoint id
_runtime_accelerator = None   # str | None — currently-bound accelerator
_runtime_nbh = None           # uuid.UUID — stable per-process notebook hash


# In-memory cell buffer for Runtime Mode. The browser path tracks cells in the
# notebook DOM; when running browserless on the GPU VM we keep our own ordered
# list so cellIndex-based execute_cell still works.
_runtime_cells: list[str] = []


def _runtime_active() -> bool:
    return _runtime_kernel is not None


def _format_kernel_outputs(reply: dict) -> str:
    """Render jupyter_kernel_client.execute() outputs into readable text."""
    lines = []
    for out in reply.get("outputs", []):
        otype = out.get("output_type")
        if otype == "stream":
            lines.append(out.get("text", ""))
        elif otype in ("execute_result", "display_data"):
            data = out.get("data", {})
            if "text/plain" in data:
                lines.append(data["text/plain"])
        elif otype == "error":
            ename = out.get("ename", "Error")
            evalue = out.get("evalue", "")
            tb = "\n".join(out.get("traceback", []))
            lines.append(f"{ename}: {evalue}\n{tb}")
    text = "".join(lines).rstrip()
    status = reply.get("status", "ok")
    if status != "ok" and not text:
        text = f"(execution status: {status})"
    return text or "(no output)"


async def _runtime_execute(code: str, wait: float = 8.0) -> str:
    """Execute code on the assigned GPU VM kernel (Runtime Mode).

    Long-running cells (e.g. training) keep executing on the kernel, which keeps
    the Colab VM busy and prevents idle reclamation. We only WAIT up to ``wait``
    seconds for the kernel to go idle; if it is still running we return a
    non-error "still running" status so the MCP tool call returns promptly
    instead of blocking until the harness times out and retries. The cell keeps
    executing on the kernel — monitor it out-of-band (e.g. a heartbeat the cell
    pushes to external storage). This is the fix for execute_cell "Request timed
    out after N retries" on long cells.
    """
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(_runtime_kernel.execute, code, timeout=wait),
            timeout=wait + 5.0,
        )
        return _format_kernel_outputs(reply)
    except (asyncio.TimeoutError, TimeoutError):
        # A timeout means EITHER the cell is genuinely still running, OR the
        # socket is dead and the reply will never come. The old code assumed the
        # former unconditionally -> a reclaimed VM reported "still running"
        # forever (silent failure). Disambiguate with a liveness probe.
        try:
            alive = await asyncio.to_thread(_runtime_kernel.is_alive)
        except Exception:
            alive = False
        if alive:
            return (
                "[cell SUBMITTED and STILL RUNNING on the kernel — returned early so "
                "the tool call does not block. The cell keeps executing, which keeps "
                "the Colab VM busy and alive (no idle reclamation). Do NOT assume "
                "failure; monitor progress out-of-band (e.g. a heartbeat the cell "
                "writes to external storage / HF).]"
            )
        _mark_runtime_dead()
        return (
            "[kernel connection LOST while the cell was running — the Colab VM was "
            "likely reclaimed/preempted or the socket dropped. The runtime is now "
            "marked dead; call change_runtime to reconnect and relaunch.]"
        )
    except Exception as exc:
        # RuntimeError('Connection was lost.'), WebSocketConnectionClosedException,
        # etc. would otherwise propagate raw out of the tool. Report cleanly.
        logging.warning(f"runtime execute failed: {exc}")
        return f"[kernel execution error: {exc}. Call change_runtime to reconnect.]"


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
    """Add a new code cell to the Colab notebook. With a GPU runtime active (change_runtime), the cell is buffered for GPU execution; otherwise requires a browser connection."""
    if _runtime_active():
        idx = len(_runtime_cells)
        _runtime_cells.append(code)
        return f"Added code cell at index {idx} (runtime mode). Execute it with execute_cell(cellIndex={idx})."
    return await _forward_or_stub("add_code_cell", {"code": code, "cellIndex": cellIndex, "language": language})


@mcp.tool()
async def add_text_cell(content: str = "", cellIndex: int = -1) -> str:
    """Add a new text/markdown cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("add_text_cell", {"content": content, "cellIndex": cellIndex})


@mcp.tool()
async def execute_cell(cellId: str = "", cellIndex: int = 0) -> str:
    """Execute a cell in the Colab notebook. Pass cellId (from add_code_cell result) or cellIndex. With a GPU runtime active (change_runtime) the buffered cell runs on the GPU VM; otherwise requires a browser connection."""
    if _runtime_active():
        try:
            idx = int(cellId) if cellId else int(cellIndex)
        except (TypeError, ValueError):
            return f"Runtime mode: cellId must be a numeric index; got {cellId!r}."
        if idx < 0 or idx >= len(_runtime_cells):
            return (
                f"Runtime mode: no buffered cell at index {idx} "
                f"({len(_runtime_cells)} cell(s) added)."
            )
        return await _runtime_execute(_runtime_cells[idx])
    args = {}
    if cellId:
        args["cellId"] = cellId
    else:
        args["cellId"] = str(cellIndex)
    return await _forward_or_stub("run_code_cell", args)


@mcp.tool()
async def update_cell(cellId: str = "", content: str = "") -> str:
    """Update the contents of an existing cell in the Colab notebook. With a GPU runtime active (change_runtime) the buffered cell is updated; otherwise requires a browser connection."""
    if _runtime_active():
        try:
            idx = int(cellId)
        except (TypeError, ValueError):
            return f"Runtime mode: cellId must be a numeric index; got {cellId!r}."
        if idx < 0 or idx >= len(_runtime_cells):
            return (
                f"Runtime mode: no buffered cell at index {idx} "
                f"({len(_runtime_cells)} cell(s) added)."
            )
        _runtime_cells[idx] = content
        return f"Updated buffered cell at index {idx} (runtime mode)."
    return await _forward_or_stub("update_cell", {"cellId": cellId, "content": content})


def _teardown_runtime_kernel() -> None:
    """Disconnect (and shut down) the current Runtime-Mode kernel, if any."""
    global _runtime_kernel, _runtime_endpoint, _runtime_accelerator
    if _runtime_kernel is not None:
        try:
            _runtime_kernel.stop(shutdown_kernel=True)
        except Exception as exc:
            logging.warning(f"Error stopping runtime kernel: {exc}")
    _runtime_kernel = None
    _runtime_endpoint = None
    _runtime_accelerator = None
    _runtime_cells.clear()


def _mark_runtime_dead() -> None:
    """Drop the kernel reference WITHOUT shutting the kernel down.

    Used when a connection drop is detected mid-cell: the VM may still be running
    (and the cell with it), so we must NOT call stop(shutdown_kernel=True). We
    just clear our handle so a subsequent change_runtime re-assigns/re-attaches
    instead of refusing because a (dead-socket) kernel object still exists.
    """
    global _runtime_kernel, _runtime_endpoint, _runtime_accelerator
    _runtime_kernel = None
    _runtime_endpoint = None
    _runtime_accelerator = None


@mcp.tool()
async def change_runtime(accelerator: str = "T4") -> str:
    """Change the Colab runtime to use a specific GPU accelerator. Valid values: NONE, T4, L4, A100.

    On success this assigns the VM AND opens a direct Jupyter-kernel connection
    to it, so cells run on the GPU (no browser needed). Requires OAuth setup
    (first time opens browser for consent).
    """
    global _runtime_kernel, _runtime_endpoint, _runtime_accelerator, _runtime_nbh
    if _colab_client is None:
        return "Runtime API not initialized. Start with --client-oauth-config flag pointing to your OAuth client secrets JSON."
    try:
        from colab_mcp.client import Accelerator, Variant
        import uuid

        acc = Accelerator(accelerator)

        # Stable per-process notebook hash. Reusing the same hash means a repeat
        # change_runtime replaces this runtime instead of orphaning a new VM
        # (the original bug: a fresh uuid4() every call left the GPU unbound).
        if _runtime_nbh is None:
            _runtime_nbh = uuid.uuid4()

        # Reuse-if-alive: if we're already bound to the requested accelerator and
        # the kernel is still live, DO NOT tear it down — a re-entrant
        # change_runtime (e.g. an agent "reconnecting" after a transient timeout)
        # would otherwise stop(shutdown_kernel=True) and kill a running 70-min
        # training cell. Only reassign when the accelerator changed or the kernel
        # is actually dead.
        if _runtime_kernel is not None and _runtime_accelerator == accelerator:
            try:
                if _runtime_kernel.is_alive():
                    return (
                        f"Runtime already on {accelerator} with a LIVE kernel "
                        f"(endpoint {_runtime_endpoint}) — reusing it, not "
                        "restarting. Any running cell keeps executing on the GPU VM."
                    )
            except Exception:
                pass  # probe failed -> treat as dead, fall through to reassign

        # Tear down any existing Runtime-Mode kernel before reassigning.
        _teardown_runtime_kernel()

        # NONE means: release the runtime and run on no GPU.
        if acc == Accelerator.NONE:
            try:
                for a in _colab_client.list_assignments():
                    _colab_client.unassign(a.endpoint)
            except Exception as exc:
                logging.warning(f"Error unassigning runtimes: {exc}")
            return "Runtime set to NONE. GPU released; runtime kernel disconnected."

        variant = Variant.GPU

        # Assign the VM and resolve the runtime-proxy coordinates in one step.
        conn = _colab_client.assign_runtime(_runtime_nbh, variant, acc)

        if not conn.proxy_url:
            # We have a VM but no proxy URL to drive it — surface clearly rather
            # than silently returning a CPU-bound success like before.
            return (
                f"Runtime assigned (endpoint {conn.endpoint}) but the runtime "
                "proxy URL could not be resolved, so cells cannot be routed to "
                "the GPU. This usually means the assignment is still warming up "
                "— retry change_runtime in a few seconds."
            )

        # Connect a Jupyter kernel to the assigned VM through the runtime proxy.
        from jupyter_kernel_client import KernelClient

        # The Colab runtime proxy (*.prod.colab.dev) does NOT authenticate with a
        # standard `Authorization: Bearer` header — that's what made every
        # /api/kernels request 404 at the proxy edge (empty body, no
        # Content-Type: the edge rejected the request before reaching Jupyter).
        # It gates on two custom headers, exactly like the official
        # googlecolab/colab-vscode extension's `colabProxyFetch`/WebSocket:
        #   X-Colab-Runtime-Proxy-Token: <the runtime-proxy JWT>
        #   X-Colab-Client-Agent: vscode
        # KernelClient threads `headers=` through to both the HTTP fetches
        # (manager `__extra_headers`) and the kernel WebSocket handshake
        # (wsclient `header=`), so passing them here authenticates the whole
        # REST+WS data plane. (Verified live: GET /api/kernels -> 200 and a cell
        # ran `torch.cuda.is_available() == True` on a Tesla T4.)
        proxy_headers = {
            "X-Colab-Runtime-Proxy-Token": conn.proxy_token,
            "X-Colab-Client-Agent": "vscode",
        }
        kernel = KernelClient(
            server_url=conn.proxy_url,
            token=conn.proxy_token,
            headers=proxy_headers,
            # WS keepalive + auto-reconnect. Upstream default reconnect_interval=0
            # means a dropped data-plane socket is NEVER recovered -> the next
            # execute raises "Connection is already closed" and the 70-min cell is
            # lost. ping_interval keeps the socket from being idle-closed during
            # long output-silent stretches. These forward through **kwargs to the
            # KernelWebSocketClient (verified: ws ctor has both params).
            ping_interval=20,
            reconnect_interval=5,
        )
        # KernelClient network I/O is blocking; run it off the event loop.
        await asyncio.to_thread(kernel.start)

        _runtime_kernel = kernel
        _runtime_endpoint = conn.endpoint
        _runtime_accelerator = accelerator

        return (
            f"Runtime changed to {accelerator} and a kernel is connected to the "
            f"GPU VM (endpoint {conn.endpoint}). Cells you execute now run on the "
            "GPU directly — no browser connection required. (You can still call "
            "open_colab_browser_connection to view the notebook in a browser.)"
        )
    except Exception as e:
        _teardown_runtime_kernel()
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
        _teardown_runtime_kernel()
        if args.enable_proxy and _session_mcp:
            await _session_mcp.cleanup()
        # Always unregister so a clean shutdown doesn't leave a stale entry.
        try:
            process_registry.unregister()
        except Exception as exc:
            logging.warning(f"Could not unregister process: {exc}")


def main() -> None:
    asyncio.run(main_async())
