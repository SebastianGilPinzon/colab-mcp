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
import tempfile
import sys
import webbrowser

from fastmcp import FastMCP
from fastmcp.utilities import logging as fastmcp_logger

from colab_mcp.session import ColabSessionProxy, NOT_CONNECTED_MSG
from colab_mcp.websocket_server import COLAB, SCRATCH_PATH


mcp = FastMCP(name="ColabMCP")

# These will be set during main_async() startup
_proxy_client = None
_session_mcp = None


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
    else:
        return "Connection timed out. Please make sure you have a Colab notebook open in your browser and try again."


@mcp.tool()
async def add_code_cell(code: str = "", cellIndex: int = -1) -> str:
    """Add a new code cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("add_code_cell", {"code": code, "cellIndex": cellIndex})


@mcp.tool()
async def add_text_cell(content: str = "", cellIndex: int = -1) -> str:
    """Add a new text/markdown cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("add_text_cell", {"content": content, "cellIndex": cellIndex})


@mcp.tool()
async def execute_cell(cellIndex: int = 0) -> str:
    """Execute a cell in the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("execute_cell", {"cellIndex": cellIndex})


@mcp.tool()
async def update_cell(cellId: str = "", content: str = "") -> str:
    """Update the contents of an existing cell in the Colab notebook. Requires an active browser connection via open_colab_browser_connection."""
    return await _forward_or_stub("update_cell", {"cellId": cellId, "content": content})


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
    return parser.parse_args(v)


async def main_async():
    global _proxy_client, _session_mcp
    args = parse_args(sys.argv[1:])
    init_logger(args.log)

    if args.enable_proxy:
        logging.info("enabling session proxy tools")
        _session_mcp = ColabSessionProxy()
        await _session_mcp.start_proxy_server()
        _proxy_client = _session_mcp.proxy_client

    try:
        await mcp.run_async()

    finally:
        if args.enable_proxy and _session_mcp:
            await _session_mcp.cleanup()


def main() -> None:
    asyncio.run(main_async())
