import asyncio
from unittest import mock
import fastmcp
from colab_mcp import session
import mcp.types as types
from fastmcp.server.middleware import MiddlewareContext
from mcp.client.session import ClientSession
import websockets
from colab_mcp.websocket_server import ColabWebSocketServer
import socket
from contextlib import closing

import pytest


@pytest.fixture
def free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@pytest.fixture
def mock_wss():
    """Provides a mock ColabWebSocketServer instance."""
    return MockColabWebSocketServer()


class MockColabWebSocketServer:
    def __init__(self):
        self.connection_live = asyncio.Event()
        self.read_stream = mock.AsyncMock()
        self.write_stream = mock.AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def ws_send_json(ws, msg: types.JSONRPCMessage):
    ws.send(msg.model_dump_json(by_alias=True, exclude_none=True))


async def fakeFrontendConnection(port):
    async with websockets.connect(
        f"ws://localhost:{port}",
        origin="https://colab.google.com",
        subprotocols=["mcp"],
    ) as ws:
        # send notification on connect
        notification = types.ToolListChangedNotification()
        ws_send_json(ws, notification)
        async for message in ws:
            rpc = types.JSONRPCMessage.model_validate_json(message)
            match rpc:
                case types.CallToolRequest():
                    resp = types.CallToolResult(id=rpc.id)
                    result = types.TextContent("a tool result")
                    resp.content = [result]
                    ws_send_json(ws, resp)
                case types.ListToolsRequest():
                    tool_a = types.Tool(name="tool_a")
                    tool_b = types.Tool(name="tool_b")
                    resp = types.ListToolsResult(id=rpc.id, tools=[tool_a, tool_b])
                    ws_send_json(ws, resp)


class TestColabProxyMiddleware:
    @pytest.mark.asyncio
    async def test_connection_live(self, mock_wss):
        mock_wss.connection_live.set()
        middleware = session.ColabProxyMiddleware(mock_wss)
        context = mock.Mock(spec=MiddlewareContext)
        call_next = mock.AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)

    @pytest.mark.asyncio
    async def test_connection_not_live(self, mock_wss):
        middleware = session.ColabProxyMiddleware(mock_wss)
        context = mock.Mock(spec=MiddlewareContext)
        call_next = mock.AsyncMock()

        with pytest.raises(Exception, match="No Colab browser session is connected."):
            await middleware.on_message(context, call_next)


class TestColabProxyClient:
    def test_client_factory_connection_live(self, mock_wss):
        mock_wss.connection_live.set()
        client = session.ColabProxyClient(mock_wss)
        client.proxy_mcp_client = mock.Mock()

        assert client.client_factory() is client.proxy_mcp_client

    def test_client_factory_connection_not_live(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.client_factory() is client.stubbed_mcp_client

    @pytest.mark.asyncio
    @mock.patch("colab_mcp.session.Client")
    @mock.patch("colab_mcp.session.ColabTransport", spec=session.ColabTransport)
    async def test_start_proxy_client(self, mock_colab_transport, mock_client, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        async with client:
            await client._start_task

        mock_colab_transport.assert_called_once_with(mock_wss)
        mock_client.assert_any_call(mock_colab_transport.return_value)


class TestColabSessionProxy:
    @pytest.mark.asyncio
    @mock.patch("colab_mcp.session.ColabProxyClient")
    @mock.patch("colab_mcp.session.ColabProxyMiddleware")
    async def test_start_proxy_server(
        self, mock_colab_proxy_client, mock_colab_proxy_middleware
    ):
        proxy = session.ColabSessionProxy()
        await proxy.start_proxy_server()
        mock_colab_proxy_client.assert_called_once()
        assert proxy.proxy_server is not None
        mock_colab_proxy_middleware.assert_called_once()