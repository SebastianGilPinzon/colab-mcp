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

import asyncio
from collections.abc import AsyncIterator
import contextlib
from contextlib import AsyncExitStack
import logging
from fastmcp import FastMCP, Client
from fastmcp.client.transports import ClientTransport
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
from fastmcp.server.proxy import FastMCPProxy
from fastmcp.tools.tool import Tool, ToolResult
from mcp.client.session import ClientSession
from mcp.types import TextContent
import webbrowser

from colab_mcp.websocket_server import ColabWebSocketServer, COLAB, SCRATCH_PATH

logger = logging.getLogger(__name__)

UI_CONNECTION_TIMEOUT = 60.0  # secs
TOOLS_READY_TIMEOUT = 10.0  # secs
TOOLS_READY_POLL_INTERVAL = 0.5  # secs

INJECTED_TOOL_NAME = "open_colab_browser_connection"

NOT_CONNECTED_MSG = (
    "Not connected to a Google Colab browser session. "
    "Please call open_colab_browser_connection first to establish a connection, "
    "then retry this tool."
)


def _make_stub_server() -> FastMCP:
    """Create an empty FastMCP server used as fallback when no browser is connected.

    The actual stub tools are provided by ToolInjectionMiddleware via
    _make_injected_tools(). This server must remain empty to avoid
    duplicate tool names (the proxy's ProxyToolManager merges tools from
    this server with those from the middleware, so any tools defined here
    would appear twice in tools/list).
    """
    return FastMCP("colab-notebook-stubs")


class ColabTransport(ClientTransport):
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss

    @contextlib.asynccontextmanager
    async def connect_session(self, **session_kwargs) -> AsyncIterator[ClientSession]:
        async with ClientSession(
            self.wss.read_stream, self.wss.write_stream, **session_kwargs
        ) as session:
            yield session

    def __repr__(self) -> str:
        return "<ColabSessionProxyTransport>"


class ColabProxyClient:
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss
        self.stubbed_mcp_client = Client(_make_stub_server())
        self.proxy_mcp_client: Client | None = None
        self._exit_stack = AsyncExitStack()
        self._start_task = None

    def is_connected(self):
        return self.wss.connection_live.is_set() and self.proxy_mcp_client is not None

    async def await_proxy_connection(self):
        with contextlib.suppress(asyncio.TimeoutError):
            # wait for the connection to be live and for the proxy client to fully initialize
            connection_tasks = asyncio.gather(
                self.wss.connection_live.wait(), self._start_task
            )
            await asyncio.wait_for(
                connection_tasks,
                timeout=UI_CONNECTION_TIMEOUT,
            )

    async def await_tools_ready(self) -> list[str]:
        """Poll the proxy client until remote tools are available."""
        if not self.is_connected():
            return []
        elapsed = 0.0
        while elapsed < TOOLS_READY_TIMEOUT:
            try:
                tools = await self.proxy_mcp_client.list_tools()
                if tools:
                    return [t.name for t in tools]
            except Exception:
                pass
            await asyncio.sleep(TOOLS_READY_POLL_INTERVAL)
            elapsed += TOOLS_READY_POLL_INTERVAL
        return []

    def client_factory(self):
        if self.is_connected():
            return self.proxy_mcp_client
        # return a client mapped to a stubbed mcp server if there is no session proxy
        return self.stubbed_mcp_client

    async def _start_proxy_client(self):
        # blocks until a websocket connection is made successfully
        self.proxy_mcp_client = await self._exit_stack.enter_async_context(
            Client(ColabTransport(self.wss))
        )

    async def __aenter__(self):
        self._start_task = asyncio.create_task(self._start_proxy_client())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._start_task:
            self._start_task.cancel()
        await self._exit_stack.aclose()


class ColabProxyMiddleware(Middleware):
    def __init__(self, proxy_client: ColabProxyClient):
        self.proxy_client = proxy_client
        self.last_message_connected = self.proxy_client.is_connected()

    async def on_message(self, context: MiddlewareContext, call_next):
        """
        Check for a change to Colab session connectivity on any communication with this MCP server and
        notify the client when the connectivity status has changed.
        """
        result = await call_next(context)

        connected = self.proxy_client.is_connected()
        connection_state_changed = connected != self.last_message_connected
        self.last_message_connected = connected
        if connection_state_changed:
            await context.fastmcp_context.send_tool_list_changed()

        return result

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if context.message.name != INJECTED_TOOL_NAME:
            return result
        if self.proxy_client.is_connected():
            return result
        # if the tool call was for open_colab_browser_connection and there is no existing connection, try to await full connection
        await context.fastmcp_context.report_progress(
            progress=1, total=4, message="The user is not connected to the Colab UI"
        )
        await context.fastmcp_context.report_progress(
            progress=2,
            total=4,
            message="Waiting for user to connect in Colab - will wait for 60s",
        )
        await self.proxy_client.await_proxy_connection()
        if self.proxy_client.is_connected():
            await context.fastmcp_context.report_progress(
                progress=3,
                total=4,
                message="Connected! Waiting for notebook tools to become available...",
            )
            tool_names = await self.proxy_client.await_tools_ready()
            tools_text = ", ".join(tool_names) if tool_names else "none discovered"
            await context.fastmcp_context.report_progress(
                progress=4,
                total=4,
                message=f"Ready! Available tools: {tools_text}",
            )
            await context.fastmcp_context.send_tool_list_changed()
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Connection successful. Available notebook tools: {tools_text}. You can now create, edit, and execute cells in the Colab notebook.",
                    )
                ],
                structured_content={
                    "result": True,
                    "available_tools": tool_names,
                },
            )
        else:
            await context.fastmcp_context.report_progress(
                progress=4,
                total=4,
                message="Timeout while waiting for the user to connect.",
            )
            return ToolResult(
                content=[TextContent(type="text", text="false")],
                structured_content={"result": False},
            )


def _make_injected_tools(
    proxy_client: ColabProxyClient,
) -> list[Tool]:
    """Create all injected tools: connection tool + notebook stub tools.

    The stub tools are pre-registered so MCP clients that snapshot tools at
    startup can discover them immediately. When the browser is not connected,
    they return a helpful message. When connected, the proxy forwards calls
    to the real browser-side MCP transparently (the middleware intercepts
    calls to stub tool names and delegates to the proxy when connected).
    """

    async def check_session_proxy_tool_fn() -> bool:
        if proxy_client.is_connected():
            return True
        webbrowser.open_new(
            f"{COLAB}{SCRATCH_PATH}#mcpProxyToken={proxy_client.wss.token}&mcpProxyPort={proxy_client.wss.port}"
        )
        return False

    async def add_code_cell_stub(code: str = "", cellIndex: int = -1) -> str:
        return NOT_CONNECTED_MSG

    async def add_text_cell_stub(content: str = "", cellIndex: int = -1) -> str:
        return NOT_CONNECTED_MSG

    async def execute_cell_stub(cellIndex: int = 0) -> str:
        return NOT_CONNECTED_MSG

    async def update_cell_stub(cellId: str = "", content: str = "") -> str:
        return NOT_CONNECTED_MSG

    return [
        Tool.from_function(
            fn=check_session_proxy_tool_fn,
            name=INJECTED_TOOL_NAME,
            description="Opens a connection to a Google Colab browser session and unlocks notebook editing tools. Returns a boolean representing whether the connection attempt succeeded",
        ),
        Tool.from_function(
            fn=add_code_cell_stub,
            name="add_code_cell",
            description="Add a new code cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection.",
        ),
        Tool.from_function(
            fn=add_text_cell_stub,
            name="add_text_cell",
            description="Add a new text/markdown cell to the Colab notebook. Requires an active browser connection via open_colab_browser_connection.",
        ),
        Tool.from_function(
            fn=execute_cell_stub,
            name="execute_cell",
            description="Execute a cell in the Colab notebook. Requires an active browser connection via open_colab_browser_connection.",
        ),
        Tool.from_function(
            fn=update_cell_stub,
            name="update_cell",
            description="Update the contents of an existing cell in the Colab notebook. Requires an active browser connection via open_colab_browser_connection.",
        ),
    ]


class ColabSessionProxy:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.proxy_client: ColabProxyClient | None = None
        self.wss: ColabWebSocketServer | None = None

    async def start_proxy_server(self):
        self.wss = await self._exit_stack.enter_async_context(ColabWebSocketServer())
        self.proxy_client = await self._exit_stack.enter_async_context(
            ColabProxyClient(self.wss)
        )

    async def cleanup(self):
        await self._exit_stack.aclose()
