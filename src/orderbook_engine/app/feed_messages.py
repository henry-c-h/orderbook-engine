"""The messages of the market data feed, and their JSON encoding.

The wire format is specified in docs/FEED.md. This module is pure: it holds the
message types and turns a message into text, and does no I/O.

A message never carries an order id, a participant id or a log seq: the types
below have no field for them, so a message cannot leak them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import assert_never

from orderbook_engine.domain.models import Side, is_pos_int


def _is_non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """The total resting quantity at one price."""

    price: int
    quantity: int

    def __post_init__(self) -> None:
        if not is_pos_int(self.price) or not is_pos_int(self.quantity):
            raise ValueError("price and quantity of a level must be positive ints")


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The whole book as of feed message `seq`. Both sides list the best price first.

    `seq` is 0 for a feed that has published nothing. A snapshot uses no number.
    """

    seq: int
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]

    def __post_init__(self) -> None:
        if not _is_non_negative_int(self.seq):
            raise ValueError(f"seq must be a non-negative int, got {self.seq!r}")


@dataclass(frozen=True, slots=True)
class LevelUpdate:
    """The new total quantity at a price. Quantity 0 removes the level."""

    seq: int
    side: Side
    price: int
    quantity: int

    def __post_init__(self) -> None:
        if not is_pos_int(self.seq):
            raise ValueError(f"seq must be a positive int, got {self.seq!r}")
        if not is_pos_int(self.price):
            raise ValueError(f"price must be a positive int, got {self.price!r}")
        if not _is_non_negative_int(self.quantity):
            raise ValueError(
                f"quantity must be a non-negative int, got {self.quantity!r}"
            )


@dataclass(frozen=True, slots=True)
class FeedTrade:
    """An execution, at the resting order's price."""

    seq: int
    trade_id: int
    price: int
    quantity: int
    aggressor_side: Side

    def __post_init__(self) -> None:
        for name in ("seq", "trade_id", "price", "quantity"):
            value = getattr(self, name)
            if not is_pos_int(value):
                raise ValueError(f"{name} must be a positive int, got {value!r}")


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """The last number assigned. A heartbeat uses no number."""

    seq: int

    def __post_init__(self) -> None:
        if not _is_non_negative_int(self.seq):
            raise ValueError(f"seq must be a non-negative int, got {self.seq!r}")


type FeedMessage = Snapshot | LevelUpdate | FeedTrade | Heartbeat


def encode_message(message: FeedMessage) -> str:
    """The message as compact JSON text."""
    return json.dumps(_to_json(message), separators=(",", ":"))


def _levels(levels: tuple[PriceLevel, ...]) -> list[dict[str, int]]:
    return [{"price": level.price, "quantity": level.quantity} for level in levels]


def _to_json(message: FeedMessage) -> dict[str, object]:
    match message:
        case Snapshot():
            return {
                "type": "snapshot",
                "seq": message.seq,
                "bids": _levels(message.bids),
                "asks": _levels(message.asks),
            }
        case LevelUpdate():
            return {
                "type": "level",
                "seq": message.seq,
                "side": message.side.value,
                "price": message.price,
                "quantity": message.quantity,
            }
        case FeedTrade():
            return {
                "type": "trade",
                "seq": message.seq,
                "trade_id": message.trade_id,
                "price": message.price,
                "quantity": message.quantity,
                "aggressor_side": message.aggressor_side.value,
            }
        case Heartbeat():
            return {"type": "heartbeat", "seq": message.seq}
        case _:
            assert_never(message)
