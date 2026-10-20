"""A feed client that stops reading, against a real server on a real socket.

The other route tests run inside Starlette's test client, where nothing can
fill a socket buffer. Here uvicorn serves the app on a loopback port, one client
reads normally (through the `websockets` library) and one is a raw socket that
completes the handshake and then never reads. Both socket buffers are made
small, so a few thousand messages are enough to fill them.

What this shows: the feed drops the stalled client, the sequencer keeps
acknowledging commands, the reading client misses nothing, and a stalled client
that starts reading soon enough gets an unbroken start and then code 4001.
What it does not show: that a client which never reads again is told 4001. A
close frame cannot reach a peer that is not reading. The server queues it behind
the unread data, and uvicorn later gives up on the connection (a probe that
waited 15 s without reading saw the connection end with no close frame).
"""

import asyncio
import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress

import uvicorn
from builders import buy
from fastapi import FastAPI
from feed_client import FeedClient
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from orderbook_engine.adapters.api.app import create_app
from orderbook_engine.app.feed import Feed
from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.app.sequencer import Sequencer
from orderbook_engine.domain.commands import Submit

BUFFER_BYTES = 4096
QUEUE_SIZE = 100
MOST_COMMANDS = 200_000
SECONDS = 20.0


@asynccontextmanager
async def listening(app: FastAPI) -> AsyncGenerator[int]:
    """Serve `app` with uvicorn on a free loopback port; yields the port."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Accepted connections inherit this, so the server's side fills quickly too.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BUFFER_BYTES)
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await until(lambda: server.started)
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait({serving}, timeout=SECONDS)
        sock.close()


async def until(condition: Callable[[], bool]) -> None:
    """Wait for `condition`, or fail after `SECONDS`."""
    async with asyncio.timeout(SECONDS):
        while not condition():
            await asyncio.sleep(0.005)


async def follow(connection: ClientConnection, client: FeedClient) -> None:
    """Feed everything the connection receives to `client`."""
    with suppress(ConnectionClosed):
        async for text in connection:
            assert isinstance(text, str)
            client.receive_text(text)


async def flood(sequencer: Sequencer, feed: Feed) -> int:
    """Submit buys until the feed drops a subscriber; returns how many.

    Each submit is awaited, so a sequencer that stalled would fail the 5 s limit.
    A short pause every 20 commands lets the reading client keep up.
    """
    sent = 0
    async with asyncio.timeout(SECONDS):
        while feed.subscriber_count == 2 and sent < MOST_COMMANDS:
            await asyncio.wait_for(sequencer.submit(Submit(buy(f"o{sent}", 100))), 5)
            sent += 1
            if sent % 20 == 0:
                await asyncio.sleep(0.002)
    return sent


class StalledClient:
    """A WebSocket client that completes the handshake and never reads again."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_BYTES)
        self._socket.setblocking(False)
        self._buffer = bytearray()

    async def connect(self, port: int) -> None:
        await self._loop.sock_connect(self._socket, ("127.0.0.1", port))
        request = (
            "GET /feed HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        await self._loop.sock_sendall(self._socket, request.encode())
        while b"\r\n\r\n" not in self._buffer:
            await self._fill()
        head, _, rest = bytes(self._buffer).partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101"), head
        self._buffer = bytearray(rest)

    async def read_until_closed(self) -> tuple[list[str], int | None]:
        """The text messages up to the close frame, and its code (None on EOF)."""
        texts: list[str] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                if not await self._fill():
                    return texts, None
                continue
            opcode, payload = frame
            if opcode == 0x1:
                texts.append(payload.decode())
            elif opcode == 0x8:
                return texts, int.from_bytes(payload[:2], "big")

    async def _fill(self) -> bool:
        async with asyncio.timeout(SECONDS):
            chunk = await self._loop.sock_recv(self._socket, 65536)
        self._buffer += chunk
        return bool(chunk)

    def _take_frame(self) -> tuple[int, bytes] | None:
        """One unmasked server frame from the buffer, or None if incomplete."""
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        length = buffer[1] & 0x7F
        offset = 2
        if length == 126:
            offset = 4
        elif length == 127:
            offset = 10
        if len(buffer) < offset:
            return None
        if offset > 2:
            length = int.from_bytes(buffer[2:offset], "big")
        if len(buffer) < offset + length:
            return None
        opcode = buffer[0] & 0x0F
        payload = bytes(buffer[offset : offset + length])
        del buffer[: offset + length]
        return opcode, payload

    def close(self) -> None:
        self._socket.close()


class TestSlowReader:
    async def test_a_reader_that_stops_is_dropped_and_nobody_else_notices(self) -> None:
        # given a real server with a small feed queue, one client that reads
        # and one that completes the handshake and then never reads
        app = create_app(InMemoryLog(), feed_queue_size=QUEUE_SIZE)
        async with listening(app) as port:
            feed: Feed = app.state.feed
            sequencer: Sequencer = app.state.sequencer
            stalled = StalledClient()
            fast = FeedClient()
            try:
                async with connect(f"ws://127.0.0.1:{port}/feed") as reader:
                    following = asyncio.create_task(follow(reader, fast))
                    try:
                        await stalled.connect(port)
                        await until(lambda: feed.subscriber_count == 2)

                        # when commands are flooded in until the feed drops a client
                        sent = await flood(sequencer, feed)
                        await until(lambda: fast.seq == feed.seq)

                        # then one client was dropped, and every command was acked
                        assert feed.subscriber_count == 1, f"nobody dropped in {sent}"
                        assert feed.seq == sent

                        # and the client that reads has every message, in order
                        assert not following.done()
                        assert fast.book == ({100: 10 * sent}, {})

                        # and when the stalled client starts reading, it gets an
                        # unbroken start, missing only what was dropped from its
                        # queue, and then the close frame with code 4001
                        texts, code = await stalled.read_until_closed()
                        late = FeedClient()
                        for text in texts:
                            late.receive_text(text)
                        assert code == 4001
                        assert late.seq is not None
                        assert sent - QUEUE_SIZE - 10 <= late.seq < sent
                    finally:
                        following.cancel()
                        await asyncio.wait({following})
            finally:
                stalled.close()

            # and a client that reconnects gets a snapshot of the whole book
            async with connect(f"ws://127.0.0.1:{port}/feed") as again:
                resynced = FeedClient()
                resynced.receive_text(str(await again.recv()))
            assert resynced.seq == sent
            assert resynced.book == ({100: 10 * sent}, {})
