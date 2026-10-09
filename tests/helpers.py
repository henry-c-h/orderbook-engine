"""Test helpers that read the book's private state directly.

This is deliberate, and the one place tests look inside the book: it keeps the
invariant checks independent of `OrderBook.snapshot()`, so that a mistake in
the snapshot cannot hide a mistake in the book.
"""

from collections import OrderedDict
from dataclasses import dataclass

from sortedcontainers import SortedDict

from orderbook_engine.domain.models import RestingOrder, Side
from orderbook_engine.engine.order_book import OrderBook


@dataclass(frozen=True, slots=True)
class RestingState:
    """One resting order as it sits on the book at the moment of the snapshot."""

    order_id: str
    participant_id: str
    side: Side
    price: int
    seq: int
    total_quantity: int
    remaining: int


@dataclass(frozen=True, slots=True)
class BookState:
    """A copy of the book's state that later changes to the book do not alter.

    Bids are best price first and asks lowest price first. Orders at one
    price are oldest first.
    """

    bids: tuple[RestingState, ...]
    asks: tuple[RestingState, ...]
    next_trade_id: int

    @property
    def orders(self) -> dict[str, RestingState]:
        """Every resting order, by order id."""
        return {order.order_id: order for order in (*self.bids, *self.asks)}


def book_state(book: OrderBook) -> BookState:
    """Copy the book's resting orders and trade counter."""
    return BookState(
        bids=_resting_states(book._bids, best_price_last=True),  # pyright: ignore[reportPrivateUsage]
        asks=_resting_states(book._asks, best_price_last=False),  # pyright: ignore[reportPrivateUsage]
        next_trade_id=book._next_trade_id,  # pyright: ignore[reportPrivateUsage]
    )


def _resting_states(
    levels: SortedDict[int, OrderedDict[str, RestingOrder]], *, best_price_last: bool
) -> tuple[RestingState, ...]:
    """List the orders of one side, best price first, oldest first within a price."""
    priced_levels = list(levels.items())
    if best_price_last:
        priced_levels.reverse()
    return tuple(
        RestingState(
            order_id=order_id,
            participant_id=resting.order.participant_id,
            side=resting.order.side,
            price=price,
            seq=resting.seq,
            total_quantity=resting.order.quantity,
            remaining=resting.remaining,
        )
        for price, level in priced_levels
        for order_id, resting in level.items()
    )


def assert_index_matches_levels(book: OrderBook) -> None:
    """The id index and the price levels hold the same orders, each exactly once."""
    index = book._orders  # pyright: ignore[reportPrivateUsage]

    ids_in_levels: list[str] = []
    for levels in (book._bids, book._asks):  # pyright: ignore[reportPrivateUsage]
        for level in levels.values():
            for order_id, resting in level.items():
                assert index.get(order_id) is resting, (
                    f"{order_id} is in a level but not in the id index"
                )
                ids_in_levels.append(order_id)

    assert len(ids_in_levels) == len(set(ids_in_levels)), (
        "an order id appears in more than one place in the levels"
    )
    assert set(index) == set(ids_in_levels), (
        "the id index and the levels hold different order ids"
    )


def assert_levels_well_formed(book: OrderBook) -> None:
    """No level is empty, and every order in a level belongs there.

    An order sits at its own limit price, on its own side of the book, with
    a remaining quantity of at least 1 and at most its total quantity.
    """
    sides = (
        (book._bids, Side.BUY),  # pyright: ignore[reportPrivateUsage]
        (book._asks, Side.SELL),  # pyright: ignore[reportPrivateUsage]
    )
    for levels, side in sides:
        for price, level in levels.items():
            assert level, "an empty price level was left on the book"
            for order_id, resting in level.items():
                assert resting.limit_price == price, (
                    f"{order_id} sits at level {price} "
                    f"but its limit price is {resting.limit_price}"
                )
                assert resting.order.side is side, (
                    f"{order_id} is a {resting.order.side} order on the {side} side"
                )
                assert 0 < resting.remaining <= resting.order.quantity, (
                    f"{order_id} has remaining {resting.remaining}, "
                    f"outside 1..{resting.order.quantity}"
                )


def assert_priority_order(book: OrderBook) -> None:
    """Orders within a level are in strictly increasing seq order."""
    for levels in (book._bids, book._asks):  # pyright: ignore[reportPrivateUsage]
        for level in levels.values():
            previous_seq = 0
            for order_id, resting in level.items():
                assert resting.seq > previous_seq, (
                    f"{order_id} is not in increasing seq order within its level"
                )
                previous_seq = resting.seq


def assert_not_crossed(book: OrderBook) -> None:
    """The best bid is below the best ask."""
    if book.best_bid is not None and book.best_ask is not None:
        assert book.best_bid < book.best_ask, "the book is crossed"


def assert_book_consistent(book: OrderBook) -> None:
    """Every structural rule of the book holds.

    The structural checks run before the priority and crossed checks, so a
    broken structure is reported as such.
    """
    assert_index_matches_levels(book)
    assert_levels_well_formed(book)
    assert_priority_order(book)
    assert_not_crossed(book)
