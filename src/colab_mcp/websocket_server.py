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

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
import asyncio
import logging
import mcp.types as types
from mcp.shared.message import SessionMessage
from pydantic_core import ValidationError
import secrets
import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol


COLAB = "https://colab.research.google.com"
COLAB_ALT_DOMAIN = "https://colab.google.com"
SCRATCH_PATH = "/notebooks/empty.ipynb"


class ColabWebSocketServer:
    """
    A WebSocket server designed to accept a single connection specifically
    from a Google Colab session (colab.google.com).
    """

    def __init__(self, host="127.0.0.1"):
        # IMPORTANT: default is "127.0.0.1" (IPv4-only), not "localhost".
        # With host="localhost" + port=0, the websockets library binds
        # dual-stack — IPv4 and IPv6 each get DIFFERENT ephemeral ports.
        # The server then reports only one of them, but Chrome resolves
        # "localhost" preferring IPv4 on Windows, so it connects to the
        # wrong port (no listener) — connection drops with
        # "stream ends after 0 bytes" and the Colab tab shows
        # "Disconnected from the local Colab MCP server".
        # Forcing IPv4-only binds a single socket on a single port, which
        # is what the Colab tab actually reaches via `ws://localhost:<port>`.
        self.host = host
        self.port = 0
        self.connection_lock = asyncio.Lock()
        self.connection_live = asyncio.Event()
        self.allowed_origins = [COLAB, COLAB_ALT_DOMAIN]
        self._server: websockets.Server | None = None

        self.read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
        self._read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
        self.write_stream: MemoryObjectSendStream[SessionMessage]
        self._write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

        self._read_stream_writer, self.read_stream = anyio.create_memory_object_stream(
            0
        )
        self.write_stream, self._write_stream_reader = (
            anyio.create_memory_object_stream(0)
        )
        self.token = secrets.token_urlsafe(16)

    async def _read_from_socket(self, websocket):
        """Listens to the socket and puts messages into the read stream."""
        async for msg in websocket:
            try:
                client_message = types.JSONRPCMessage.model_validate_json(msg)
            except ValidationError as exc:
                await self._read_stream_writer.send(exc)
                continue
            await self._read_stream_writer.send(SessionMessage(client_message))

    async def _write_to_socket(self, websocket):
        """Reads from the write stream and sends over the socket."""
        try:
            while True:
                # Wait for a message from the application
                msg = await self._write_stream_reader.receive()

                try:
                    json_obj = msg.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await websocket.send(json_obj)
                except ConnectionClosed:
                    break
        except (anyio.ClosedResourceError, anyio.EndOfStream):
            # server closed write stream
            pass

    def _validate_authorization(self, websocket: ServerConnection, request: Request):
        if request.path.find(f"access_token={self.token}") != -1:
            return None
        try:
            headers: Headers = request.headers
            auth_header = headers.get("Authorization")
            if not auth_header:
                return Response(401, "Missing authorization", Headers([]))
            scheme, token = auth_header.split(None, 1)
            if scheme.lower() != "bearer":
                return Response(400, "Invalid authorization header", Headers([]))
        except ValueError:
            return Response(400, "Invalid header format", Headers([]))
        if token == self.token:
            return None
        return Response(403, "Bad authorization token", Headers([]))

    async def _connection_handler(self, websocket: ServerConnection):
        """
        Handles incoming websocket connections.
        Validates Origin and ensures single-client exclusivity.
        """
        if self.connection_lock.locked():
            logging.warning(
                f"Connection rejected: {websocket.remote_address}. A client is already connected"
            )
            await websocket.close(code=1013, reason="Server is busy")
            return

        async with self.connection_lock:
            try:
                self.connection_live.set()

                reading_task = asyncio.create_task(self._read_from_socket(websocket))
                writing_task = asyncio.create_task(self._write_to_socket(websocket))
                _, pending = await asyncio.wait(
                    [reading_task, writing_task], return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel()

            except websockets.exceptions.ConnectionClosed as e:
                logging.info(f"Connection closed: {e.code} - {e.reason}")
                await self._read_stream_writer.send(
                    Exception("Colab Frontend disconnected")
                )
            except Exception as e:
                logging.error(f"Unexpected error: {e}")
            finally:
                self.connection_live.clear()

    async def __aenter__(self):
        self._server = await websockets.serve(
            self._connection_handler,
            host=self.host,
            port=0,
            subprotocols=[Subprotocol("mcp")],
            origins=self.allowed_origins,
            process_request=self._validate_authorization,
        )

        # Defense against the dual-stack bind bug: with host="localhost"
        # and port=0, websockets binds IPv4 and IPv6 on DIFFERENT ephemeral
        # ports, then we report only one. The Colab tab connects via
        # ws://localhost:<port> and Chrome may resolve to whichever address
        # family lost the lottery — connection refused, the tab shows
        # "Disconnected from the local Colab MCP server", and the user
        # waits 60s for a generic timeout.
        #
        # We force IPv4-only (host="127.0.0.1") above, but defend against
        # surprises here too: every socket the server bound MUST share the
        # same port, or we refuse to start.
        ports = {s.getsockname()[1] for s in self._server.sockets}
        if len(ports) != 1:
            addrs = [s.getsockname() for s in self._server.sockets]
            raise RuntimeError(
                f"WebSocket server bound to multiple ports ({sorted(ports)}); "
                f"the Colab tab can only reach one of them, so any other tab "
                f"will see 'Disconnected from the local Colab MCP server'. "
                f"Sockets: {addrs}. Set host='127.0.0.1' to avoid dual-stack "
                f"bind, or change websockets to bind a single port."
            )
        self.port = ports.pop()
        for sock in self._server.sockets:
            logging.info(f"WebSocket server listening on {sock.getsockname()}")
        logging.info(f"Colab tab will connect via ws://localhost:{self.port}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logging.info("Closing WebSocket server")
        if self._server:
            self._server.close()
            self.write_stream.close()
            self.read_stream.close()
            await self._server.wait_closed()
