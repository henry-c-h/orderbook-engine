"""The market data feed: fan-out of the log's public messages to subscribers.

The sequencer calls `publish` for each record, after the record is appended to
the log, and for each verified record when it recovers the log at start. The
feed folds the record into its own book (`FeedBook`) and puts the resulting
messages on the queue of every subscriber. Wire format and rules are in
docs/FEED.md.

Atomic connect. `publish` and `subscribe` are plain functions that never
await, and they run on the sequencer's event loop. A task switch happens only
at an `await`, so no record can be published between `subscribe` taking the
snapshot and registering the subscriber: the first message queued for a
subscriber is always the snapshot's seq plus 1. That holds only while the
sequencer runs on the same loop and `publish` stays synchronous.

Slow subscribers. Each subscriber has a bounded queue. The messages of one
record are queued together or not at all. If they do not fit, the subscriber
is dropped: its queue is discarded and it is closed with code 4001. Nothing is
conflated and `publish` never waits, so a slow client cannot delay the
sequencer or another client.

Failure. `publish` never raises. If the feed cannot apply a record, it logs
the error, closes every subscriber with code 1011 and refuses new ones. The
sequencer carries on.
"""

from __future__ import annotations

import asyncio
import logging

from orderbook_engine.app.feed_book import FeedBook
from orderbook_engine.app.feed_messages import Heartbeat, encode_message
from orderbook_engine.app.log_record import LogRecord

logger = logging.getLogger(__name__)

CLOSE_GOING_AWAY = 1001
CLOSE_INTERNAL_ERROR = 1011
CLOSE_TRY_AGAIN_LATER = 1013
CLOSE_TOO_SLOW = 4001

# Put on a subscriber's queue when it is closed, to wake a waiting `receive`.
_CLOSED = object()


class FeedUnavailableError(Exception):
    """The feed cannot take a subscriber now. `code` is the WebSocket close code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class SubscriberClosedError(Exception):
    """The subscriber was closed by the feed. `code` is the WebSocket close code."""

    def __init__(self, code: int) -> None:
        super().__init__(f"the subscriber was closed with code {code}")
        self.code = code


class Subscriber:
    """One client's view of the feed: a snapshot, then a queue of messages."""

    def __init__(self, snapshot: str) -> None:
        self.snapshot = snapshot
        self.close_code: int | None = None
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._closed = asyncio.Event()

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    async def receive(self) -> str:
        """The next message, or `SubscriberClosedError` once the subscriber is closed."""
        if self.close_code is not None:
            raise SubscriberClosedError(self.close_code)
        item = await self._queue.get()
        if item is _CLOSED:
            assert self.close_code is not None
            raise SubscriberClosedError(self.close_code)
        assert isinstance(item, str)
        return item

    async def wait_closed(self) -> int:
        """Wait until the feed closes this subscriber; returns the close code."""
        await self._closed.wait()
        assert self.close_code is not None
        return self.close_code

    def enqueue(self, texts: list[str]) -> None:
        """Queue messages. Used by the feed, which checks that they fit."""
        for text in texts:
            self._queue.put_nowait(text)

    def close(self, code: int) -> None:
        """Discard what is queued and wake any waiting `receive`.

        Used by the feed. The first call wins.
        """
        if self.close_code is not None:
            return
        self.close_code = code
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(_CLOSED)
        self._closed.set()


class Feed:
    """Folds records into the feed's book and fans the messages out."""

    def __init__(self, *, queue_size: int = 1000, max_subscribers: int = 100) -> None:
        if queue_size < 1:
            raise ValueError(f"queue_size must be at least 1, got {queue_size}")
        if max_subscribers < 1:
            raise ValueError(
                f"max_subscribers must be at least 1, got {max_subscribers}"
            )
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers
        self._book = FeedBook()
        self._subscribers: set[Subscriber] = set()
        self._closed_code: int | None = None
        self._failed = False

    @property
    def seq(self) -> int:
        """The number of the last message published."""
        return self._book.seq

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, record: LogRecord) -> None:
        """Fold `record` in and queue its messages for every subscriber.

        Never raises and never waits. Does nothing once the feed is closed.
        """
        if self._closed_code is not None:
            return
        try:
            messages = self._book.apply(record)
        except Exception:
            logger.exception(
                "the market data feed cannot apply log record %d and is now closed",
                record.seq,
            )
            self._failed = True
            self.close(CLOSE_INTERNAL_ERROR)
            return
        if not messages:
            return
        texts = [encode_message(message) for message in messages]
        for subscriber in list(self._subscribers):
            if subscriber.queued + len(texts) > self._queue_size:
                self._subscribers.discard(subscriber)
                subscriber.close(CLOSE_TOO_SLOW)
            else:
                subscriber.enqueue(texts)

    def subscribe(self) -> Subscriber:
        """A new subscriber, holding a snapshot of the feed as of `seq`.

        Raises `FeedUnavailableError` if the feed is closed or full.
        """
        if self._closed_code is not None:
            raise FeedUnavailableError("the feed is closed", self._closed_code)
        if len(self._subscribers) >= self._max_subscribers:
            raise FeedUnavailableError(
                "the feed has reached its subscriber limit", CLOSE_TRY_AGAIN_LATER
            )
        subscriber = Subscriber(encode_message(self._book.snapshot()))
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """Forget a subscriber whose client has gone."""
        self._subscribers.discard(subscriber)

    def heartbeat(self) -> str:
        """A heartbeat message for the current `seq`. It uses no number."""
        return encode_message(Heartbeat(seq=self.seq))

    def close(self, code: int) -> None:
        """Close every subscriber with `code` and refuse new ones.

        The first call wins: a closed feed keeps the code it was closed with.
        """
        if self._closed_code is not None:
            return
        self._closed_code = code
        for subscriber in self._subscribers:
            subscriber.close(code)
        self._subscribers.clear()
