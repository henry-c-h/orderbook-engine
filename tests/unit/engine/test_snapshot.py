import dataclasses

import pytest
from builders import buy, sell

from orderbook_engine.domain.models import Order
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.engine.snapshot import (
    BookSnapshot,
    LevelSnapshot,
    OrderSnapshot,
)


def book_with(*orders: Order) -> OrderBook:
    """A book where each order was submitted in turn, with seq 1, 2, 3, ..."""
    book = OrderBook()
    for seq, order in enumerate(orders, start=1):
        book.submit(order, seq)
    return book


class TestSnapshotContents:
    def test_empty_book_has_no_levels_and_the_first_trade_id_next(self) -> None:
        # given an empty book
        book = OrderBook()

        # when it is snapshotted
        snapshot = book.snapshot()

        # then there are no levels and the next trade id is 1
        assert snapshot == BookSnapshot(bids=(), asks=(), next_trade_id=1)

    def test_an_order_carries_participant_seq_total_and_remaining(self) -> None:
        # given a bid of 10 that a sell of 4 from another participant partly fills
        book = book_with(
            buy("b1", 99, participant_id="p1"),
            sell("s1", 99, quantity=4, participant_id="p2"),
        )

        # when the book is snapshotted
        snapshot = book.snapshot()

        # then the bid shows all its fields and the trade counter has advanced
        assert snapshot == BookSnapshot(
            bids=(
                LevelSnapshot(
                    price=99,
                    orders=(
                        OrderSnapshot(
                            order_id="b1",
                            participant_id="p1",
                            seq=1,
                            total_quantity=10,
                            remaining=6,
                        ),
                    ),
                ),
            ),
            asks=(),
            next_trade_id=2,
        )

    def test_total_quantity_follows_an_amend(self) -> None:
        # given a bid of 10 that is amended down to a total of 7
        book = book_with(buy("b1", 99))
        book.amend("b1", price=99, quantity=7, seq=2)

        # when the book is snapshotted
        snapshot = book.snapshot()

        # then it shows a total quantity of 7, 7 remaining and the original seq
        order = snapshot.bids[0].orders[0]
        assert (order.total_quantity, order.remaining, order.seq) == (7, 7, 1)

    def test_bids_are_listed_highest_price_first(self) -> None:
        # given bids at three prices, submitted out of order
        book = book_with(buy("b1", 98), buy("b2", 100), buy("b3", 99))

        # when the book is snapshotted
        snapshot = book.snapshot()

        # then the levels run from the highest price down
        assert [level.price for level in snapshot.bids] == [100, 99, 98]

    def test_asks_are_listed_lowest_price_first(self) -> None:
        # given asks at three prices, submitted out of order
        book = book_with(sell("s1", 102), sell("s2", 100), sell("s3", 101))

        # when the book is snapshotted
        snapshot = book.snapshot()

        # then the levels run from the lowest price up
        assert [level.price for level in snapshot.asks] == [100, 101, 102]

    def test_orders_in_a_level_are_oldest_first(self) -> None:
        # given three bids at one price
        book = book_with(buy("b1", 99), buy("b2", 99), buy("b3", 99))

        # when the book is snapshotted
        snapshot = book.snapshot()

        # then one level lists them in arrival order
        assert [level.price for level in snapshot.bids] == [99]
        assert [order.order_id for order in snapshot.bids[0].orders] == [
            "b1",
            "b2",
            "b3",
        ]

    def test_an_amend_that_loses_priority_moves_the_order_to_the_back(self) -> None:
        # given two bids at one price, the older one then amended to a higher total
        book = book_with(buy("b1", 99), buy("b2", 99))
        book.amend("b1", price=99, quantity=12, seq=3)

        # when the book is snapshotted
        snapshot = book.snapshot()

        # then b1 comes after b2 and carries the new seq
        orders = snapshot.bids[0].orders
        assert [(order.order_id, order.seq) for order in orders] == [
            ("b2", 2),
            ("b1", 3),
        ]


class TestSnapshotIsReadOnly:
    def test_snapshot_values_are_immutable(self) -> None:
        # given a snapshot of a book with one order
        snapshot = book_with(buy("b1", 99)).snapshot()
        level = snapshot.bids[0]
        order = level.orders[0]

        # when any field of the snapshot is assigned
        # then it refuses
        for value, field in (
            (snapshot, "next_trade_id"),
            (level, "price"),
            (order, "remaining"),
        ):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(value, field, 0)

    def test_snapshot_holds_tuples_only(self) -> None:
        # given a snapshot of a book with one order on each side
        snapshot = book_with(buy("b1", 99), sell("s1", 101)).snapshot()

        # when its collections are inspected
        # then every one is a tuple
        assert type(snapshot.bids) is tuple
        assert type(snapshot.asks) is tuple
        assert type(snapshot.bids[0].orders) is tuple
        assert type(snapshot.asks[0].orders) is tuple

    def test_later_book_changes_do_not_alter_an_earlier_snapshot(self) -> None:
        # given a snapshot of a book holding one bid
        book = book_with(buy("b1", 99))
        before = book.snapshot()
        expected = BookSnapshot(
            bids=(
                LevelSnapshot(
                    price=99,
                    orders=(OrderSnapshot("b1", "p1", 1, 10, 10),),
                ),
            ),
            asks=(),
            next_trade_id=1,
        )

        # when the book changes: a fill of 4, a new order and a cancel
        book.submit(sell("s1", 99, quantity=4, participant_id="p2"), 2)
        book.submit(buy("b2", 98), 3)
        book.cancel("b1")

        # then the earlier snapshot is unchanged and the new one differs
        assert before == expected
        assert book.snapshot() != before
