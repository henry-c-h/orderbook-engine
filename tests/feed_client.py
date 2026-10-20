"""A reference client for the market data feed, and the book it should end up with.

`FeedClient` follows the rules of docs/FEED.md and nothing else: it reads JSON
messages the way a real client would, so the tests exercise the wire format too.
`expected_levels` adds up the engine's own resting orders, read through
`book_state`. Tests compare the client's book with it, so a mistake in the feed
cannot hide behind the same mistake in the expected book.
"""

import json
from typing import Any

from helpers import BookState

type Levels = tuple[dict[int, int], dict[int, int]]


class FeedGapError(Exception):
    """The stream broke the rules a client relies on: it must reconnect."""


def expected_levels(state: BookState) -> Levels:
    """Total remaining quantity at each price: (bids, asks)."""
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    for orders, levels in ((state.bids, bids), (state.asks, asks)):
        for order in orders:
            levels[order.price] = levels.get(order.price, 0) + order.remaining
    return bids, asks


class FeedClient:
    """Applies feed messages to its own book and checks their sequence numbers."""

    def __init__(self) -> None:
        self.seq: int | None = None
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.trades: list[dict[str, Any]] = []

    @property
    def book(self) -> Levels:
        return dict(self.bids), dict(self.asks)

    @property
    def crossed(self) -> bool:
        return bool(self.bids) and bool(self.asks) and max(self.bids) >= min(self.asks)

    def reconnect(self) -> None:
        """Forget everything, as a client does when it opens a new connection."""
        self.seq = None
        self.bids = {}
        self.asks = {}
        self.trades = []

    def receive_text(self, text: str) -> None:
        self.receive(json.loads(text))

    def receive(self, message: dict[str, Any]) -> None:
        """Apply one message, or raise `FeedGapError`."""
        kind = message["type"]
        if self.seq is None:
            if kind != "snapshot":
                raise FeedGapError(f"the first message is a {kind}, not a snapshot")
            self.bids = {level["price"]: level["quantity"] for level in message["bids"]}
            self.asks = {level["price"]: level["quantity"] for level in message["asks"]}
            self.seq = message["seq"]
            return

        if kind == "snapshot":
            raise FeedGapError("a snapshot in the middle of a connection")
        if kind == "heartbeat":
            if message["seq"] != self.seq:
                raise FeedGapError(
                    f"heartbeat at {message['seq']}, last message was {self.seq}"
                )
            return
        if message["seq"] != self.seq + 1:
            raise FeedGapError(f"expected seq {self.seq + 1}, got {message['seq']}")
        self.seq = message["seq"]
        if kind == "trade":
            self.trades.append(message)
        elif kind == "level":
            levels = self.bids if message["side"] == "BUY" else self.asks
            if message["quantity"] == 0:
                levels.pop(message["price"], None)
            else:
                levels[message["price"]] = message["quantity"]
        else:
            raise FeedGapError(f"unknown message type {kind!r}")
