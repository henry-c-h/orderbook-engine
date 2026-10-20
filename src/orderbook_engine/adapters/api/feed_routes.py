"""The WebSocket endpoint of the market data feed.

The route only moves text between a `Subscriber` and the socket. What the
messages are, and when a subscriber is dropped, is decided in `app.feed` and
written down in docs/FEED.md.

Per connection three tasks run: one sends queued messages (and a heartbeat when
the connection has been idle for one interval), one reads and discards whatever
the client sends so a disconnect is noticed, and one waits for the feed to close
the subscriber. The first to finish ends the connection. The third task is for a
slow client: its sender may be stuck inside a send that finishes only when the
client reads, and only the feed knows that the client was dropped. Thanks to
the third task the handler moves on to close the socket with the feed's code at
once, instead of when the client next reads. A client that never reads again
is not told the code: the close frame waits behind its unread data until the
server gives up on the connection.
"""

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from orderbook_engine.app.feed import (
    Feed,
    FeedUnavailableError,
    Subscriber,
    SubscriberClosedError,
)

router = APIRouter()

CLOSE_TIMEOUT_SECONDS = 1.0


@router.websocket("/feed")
async def feed_socket(websocket: WebSocket) -> None:
    feed: Feed = websocket.app.state.feed
    heartbeat_interval: float = websocket.app.state.heartbeat_interval
    await websocket.accept()
    try:
        subscriber = feed.subscribe()
    except FeedUnavailableError as error:
        await _close(websocket, error.code)
        return

    tasks = [
        asyncio.create_task(_send(websocket, feed, subscriber, heartbeat_interval)),
        asyncio.create_task(_ignore_incoming(websocket)),
        asyncio.create_task(subscriber.wait_closed()),
    ]
    for task in tasks:
        task.add_done_callback(_consume)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # No await here: the handler may itself be cancelled at this point,
        # and the cleanup must still run to the end.
        for task in tasks:
            task.cancel()
        feed.unsubscribe(subscriber)
    if subscriber.close_code is not None:
        await _close(websocket, subscriber.close_code)


def _consume(task: asyncio.Task[Any]) -> None:
    """Mark a finished task's exception as seen, so asyncio does not log it."""
    if not task.cancelled():
        task.exception()


async def _send(
    websocket: WebSocket, feed: Feed, subscriber: Subscriber, interval: float
) -> None:
    """Send the snapshot, then every queued message, until the connection ends."""
    with contextlib.suppress(WebSocketDisconnect, SubscriberClosedError):
        await websocket.send_text(subscriber.snapshot)
        while True:
            try:
                text = await asyncio.wait_for(subscriber.receive(), timeout=interval)
            except TimeoutError:
                # The queue is empty, so every message queued so far was sent.
                text = feed.heartbeat()
            await websocket.send_text(text)


async def _ignore_incoming(websocket: WebSocket) -> None:
    """Read and discard client frames until the client disconnects."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _close(websocket: WebSocket, code: int) -> None:
    """Close the socket without waiting long: the client may not be reading."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(websocket.close(code=code), CLOSE_TIMEOUT_SECONDS)
