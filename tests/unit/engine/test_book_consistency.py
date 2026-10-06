from collections import OrderedDict

import pytest
from helpers import assert_book_consistent

from orderbook_engine.domain.models import (
    Order,
    OrderType,
    RestingOrder,
    Side,
)
from orderbook_engine.engine.order_book import OrderBook


def limit_order(*, order_id: str, side: Side, price: int) -> Order:
    return Order(
        order_id=order_id,
        participant_id="p1",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=price,
    )


class TestAssertBookConsistent:
    def test_empty_book_is_consistent(self) -> None:
        # given an empty book, then it is consistent
        assert_book_consistent(OrderBook())

    def test_book_with_both_sides_is_consistent(self) -> None:
        # given resting orders on both sides, not crossed
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="s1", side=Side.SELL, price=101), seq=2)

        # then it is consistent
        assert_book_consistent(book)

    def test_order_in_the_index_but_not_in_a_level_fails(self) -> None:
        # given a book where an order id is only in the id index
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        ghost = limit_order(order_id="ghost", side=Side.BUY, price=98)
        order_map = book._orders  # pyright: ignore[reportPrivateUsage]
        order_map["ghost"] = RestingOrder.from_order(ghost, 2)

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="different order ids"):
            assert_book_consistent(book)

    def test_order_in_a_level_but_not_in_the_index_fails(self) -> None:
        # given a book where an order is in a level but was dropped from the index
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        del book._orders["b1"]  # pyright: ignore[reportPrivateUsage]

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="not in the id index"):
            assert_book_consistent(book)

    def test_stale_copy_of_an_order_in_the_index_fails(self) -> None:
        # given a book where the index holds a different object for an id
        book = OrderBook()
        order = limit_order(order_id="b1", side=Side.BUY, price=99)
        book.submit(order, seq=1)
        order_map = book._orders  # pyright: ignore[reportPrivateUsage]
        order_map["b1"] = RestingOrder.from_order(order, 1)

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="not in the id index"):
            assert_book_consistent(book)

    def test_same_id_in_two_levels_fails(self) -> None:
        # given a book where one order object is in two levels
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        bids = book._bids  # pyright: ignore[reportPrivateUsage]
        bids[98] = OrderedDict(bids[99])

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="more than one place"):
            assert_book_consistent(book)

    def test_orders_out_of_seq_order_within_a_level_fail(self) -> None:
        # given two buys at one price, where the first now has a later seq than the second
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="b2", side=Side.BUY, price=99), seq=2)
        index = book._orders  # pyright: ignore[reportPrivateUsage]
        index["b1"].seq = 5

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="increasing seq order"):
            assert_book_consistent(book)

    def test_same_seq_twice_within_a_level_fails(self) -> None:
        # given two buys at one price with the same seq
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="b2", side=Side.BUY, price=99), seq=2)
        index = book._orders  # pyright: ignore[reportPrivateUsage]
        index["b2"].seq = 1

        # when the book is checked, then it fails: seq must be strictly increasing
        with pytest.raises(AssertionError, match="increasing seq order"):
            assert_book_consistent(book)

    def test_empty_level_fails(self) -> None:
        # given a book with an empty price level left behind
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        bids = book._bids  # pyright: ignore[reportPrivateUsage]
        bids[98] = OrderedDict()

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="empty price level"):
            assert_book_consistent(book)

    def test_crossed_book_fails(self) -> None:
        # given a book forced into a crossed state, with an ask below the bid
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="s1", side=Side.SELL, price=101), seq=2)
        asks = book._asks  # pyright: ignore[reportPrivateUsage]
        asks[98] = asks.pop(101)
        for resting in asks[98].values():
            resting.limit_price = 98

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="crossed"):
            assert_book_consistent(book)
