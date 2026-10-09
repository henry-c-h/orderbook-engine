from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    """One resting order. `seq` is its time priority; `total_quantity` includes
    anything already filled, so `remaining` is what is still on the book.
    """

    order_id: str
    participant_id: str
    seq: int
    total_quantity: int
    remaining: int


@dataclass(frozen=True, slots=True)
class LevelSnapshot:
    """The orders at one price, in priority order (oldest first)."""

    price: int
    orders: tuple[OrderSnapshot, ...]


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """A read-only copy of the book that later changes to the book do not alter.

    Both sides list the best price first: bids from the highest price down,
    asks from the lowest price up.
    """

    bids: tuple[LevelSnapshot, ...]
    asks: tuple[LevelSnapshot, ...]
    next_trade_id: int
