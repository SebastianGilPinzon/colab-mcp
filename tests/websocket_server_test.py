
import asyncio
from mcp.types import JSONRPCRequest, JSONRPCResponse, JSONRPCMessage
from mcp.shared.message import SessionMessage
from colab_mcp.websocket_server import ColabWebSocketServer
import websockets

import pytest

TEST_PORT = 9876


@pytest.mark.asyncio
async def test_successful_connection():
  async with ColabWebSocketServer(port=TEST_PORT) as server:
    client = await websockets.connect(
        f"ws://localhost:{TEST_PORT}",
        origin="colab.google.com",
        subprotocols=["mcp"],
    )
    assert server.connection_live.is_set()
    assert server.connection_lock.locked()

    await client.close()
    await client.wait_closed()
    await asyncio.sleep(1)  # Allow server to update state
    
    assert not server.connection_live.is_set()
    assert not server.connection_lock.locked()

@pytest.mark.asyncio
async def test_unauthorized_origin_rejected():
  async with ColabWebSocketServer(port=TEST_PORT) as server:
    client_conn = await websockets.connect(
        f"ws://localhost:{TEST_PORT}",
        origin="wrong.com",
        subprotocols=["mcp"],
    )
    with pytest.raises(websockets.exceptions.ConnectionClosed):
      await client_conn.ping()
    
    assert client_conn.close_code == 1008
    assert client_conn.close_reason == 'Unauthorized Origin'
    assert not server.connection_live.is_set()

@pytest.mark.asyncio
async def test_second_connection_rejected():
  async with ColabWebSocketServer(port=TEST_PORT) as server:
    client1 = await websockets.connect(
        f"ws://localhost:{TEST_PORT}",
        origin="colab.google.com",
        subprotocols=["mcp"],
    )
    assert server.connection_live.is_set()

    client2 = await websockets.connect(
        f"ws://localhost:{TEST_PORT}",
        origin="colab.google.com",
        subprotocols=["mcp"],
    )

    with pytest.raises(websockets.exceptions.ConnectionClosed, match="Server is busy", check= lambda e: e.rcvd.code==1013):
        # assert we cannot ping via the second client
        await client2.ping()

    # assert we can ping via the original client
    pong = await client1.ping()
    pong_latency = await pong
    assert pong_latency > 0
    await client1.close()

@pytest.mark.asyncio
async def test_incoming_message_handling():
  async with ColabWebSocketServer(port=TEST_PORT) as server:
    client = await websockets.connect(
        f"ws://localhost:{TEST_PORT}",
        origin="colab.google.com",
        subprotocols=["mcp"],
    )
    assert server.connection_live.is_set()

    test_message = JSONRPCResponse(
        jsonrpc="2.0",
        id="abc",
        result={"result": "success"},
    )
    await client.send(test_message.model_dump_json())

    received_msg = await asyncio.wait_for(
        server.read_stream.receive(), timeout=1
    )
    test_json_message = JSONRPCMessage(test_message)
    assert received_msg.message == test_json_message

    await client.close()

@pytest.mark.asyncio
async def test_outgoing_message_handling():
  async with ColabWebSocketServer(port=TEST_PORT) as server:
    client = await websockets.connect(
        f"ws://localhost:{TEST_PORT}",
        origin="colab.google.com",
        subprotocols=["mcp"],
    )
    assert server.connection_live.is_set()

    test_message = JSONRPCRequest(
        jsonrpc="2.0",
        id="abc",
        method="test_method",
        params={"bar": "baz"},
    )
    await server.write_stream.send(
        SessionMessage(test_message)
    )

    received_msg_str = await asyncio.wait_for(client.recv(), timeout=1)
    received_msg = JSONRPCRequest.model_validate_json(received_msg_str)
    assert received_msg == test_message

    await client.close()

@pytest.mark.asyncio
async def test_malformed_incoming_message():
  async with ColabWebSocketServer(port=TEST_PORT) as server:
    client = await websockets.connect(
        f"ws://localhost:{TEST_PORT}",
        origin="colab.google.com",
        subprotocols=["mcp"],
    )
    assert server.connection_live.is_set()

    await client.send("this is not json")

    received_item = await asyncio.wait_for(
        server.read_stream.receive(), timeout=1
    )
    assert isinstance(received_item, Exception)

    await client.close()