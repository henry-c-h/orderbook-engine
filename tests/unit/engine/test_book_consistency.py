from collections import OrderedDict
from dataclasses import replace

import pytest
from builders import limit_order
from helpers import (
    BookState,
    RestingState,
    assert_book_consistent,
    assert_index_matches_levels,
    assert_levels_well_formed,
    assert_not_crossed,
    assert_priority_order,
    book_state,
)

from orderbook_engine.domain.models import (
    RestingOrder,
    Side,
)
from orderbook_engine.engine.order_book import OrderBook


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

    def test_resting_order_with_nothing_remaining_fails(self) -> None:
        # given a resting order whose remaining quantity is zero
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        index = book._orders  # pyright: ignore[reportPrivateUsage]
        index["b1"].remaining = 0

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="remaining"):
            assert_book_consistent(book)

    def test_resting_order_with_more_remaining_than_its_quantity_fails(self) -> None:
        # given a resting order whose remaining exceeds its total quantity
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        index = book._orders  # pyright: ignore[reportPrivateUsage]
        index["b1"].remaining = 11

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="remaining"):
            assert_book_consistent(book)

    def test_order_in_a_level_that_is_not_its_price_fails(self) -> None:
        # given an order whose limit price differs from the level it sits in
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        index = book._orders  # pyright: ignore[reportPrivateUsage]
        index["b1"].limit_price = 98

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="price"):
            assert_book_consistent(book)

    def test_order_on_the_wrong_side_of_the_book_fails(self) -> None:
        # given a sell order sitting in the bids
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        index = book._orders  # pyright: ignore[reportPrivateUsage]
        resting = index["b1"]
        resting.order = replace(resting.order, side=Side.SELL)

        # when the book is checked, then it fails
        with pytest.raises(AssertionError, match="side"):
            assert_book_consistent(book)


class TestNamedChecks:
    """Each named check fails on its own rule, so a property can name the rule."""

    def test_index_check_fails_for_an_order_missing_from_the_index(self) -> None:
        # given an order in a level but not in the id index
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        del book._orders["b1"]  # pyright: ignore[reportPrivateUsage]

        # when the index check runs, then it fails
        with pytest.raises(AssertionError, match="not in the id index"):
            assert_index_matches_levels(book)

    def test_well_formed_check_fails_for_an_empty_level(self) -> None:
        # given an empty price level
        book = OrderBook()
        bids = book._bids  # pyright: ignore[reportPrivateUsage]
        bids[98] = OrderedDict()

        # when the well-formed check runs, then it fails
        with pytest.raises(AssertionError, match="empty price level"):
            assert_levels_well_formed(book)

    def test_priority_check_fails_for_orders_out_of_seq_order(self) -> None:
        # given two orders at a level where the first has the later seq
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="b2", side=Side.BUY, price=99), seq=2)
        index = book._orders  # pyright: ignore[reportPrivateUsage]
        index["b1"].seq = 5

        # when the priority check runs, then it fails
        with pytest.raises(AssertionError, match="increasing seq order"):
            assert_priority_order(book)

    def test_crossed_check_fails_for_a_crossed_book(self) -> None:
        # given a book with an ask below the bid
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="s1", side=Side.SELL, price=101), seq=2)
        asks = book._asks  # pyright: ignore[reportPrivateUsage]
        asks[98] = asks.pop(101)

        # when the crossed check runs, then it fails
        with pytest.raises(AssertionError, match="crossed"):
            assert_not_crossed(book)

    def test_each_check_passes_on_a_healthy_book(self) -> None:
        # given a healthy book with both sides
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="s1", side=Side.SELL, price=101), seq=2)

        # when each check runs, then none fails
        assert_index_matches_levels(book)
        assert_levels_well_formed(book)
        assert_priority_order(book)
        assert_not_crossed(book)


class TestBookState:
    def test_empty_book_has_no_orders_and_the_first_trade_id_next(self) -> None:
        # given an empty book
        book = OrderBook()

        # when its state is read
        state = book_state(book)

        # then nothing rests and trade ids start at 1
        assert state == BookState(bids=(), asks=(), next_trade_id=1)

    def test_bids_are_best_price_first_and_asks_lowest_price_first(self) -> None:
        # given bids at 98 and 99 and asks at 102 and 101, submitted worst first
        book = OrderBook()
        book.submit(limit_order(order_id="b98", side=Side.BUY, price=98), seq=1)
        book.submit(limit_order(order_id="b99", side=Side.BUY, price=99), seq=2)
        book.submit(limit_order(order_id="s102", side=Side.SELL, price=102), seq=3)
        book.submit(limit_order(order_id="s101", side=Side.SELL, price=101), seq=4)

        # when the state is read
        state = book_state(book)

        # then each side lists its best price first
        assert [order.order_id for order in state.bids] == ["b99", "b98"]
        assert [order.order_id for order in state.asks] == ["s101", "s102"]

    def test_orders_at_one_price_are_in_queue_order(self) -> None:
        # given two bids at one price
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="b2", side=Side.BUY, price=99), seq=2)

        # when the state is read
        state = book_state(book)

        # then they are listed oldest first
        assert [order.order_id for order in state.bids] == ["b1", "b2"]

    def test_resting_order_reports_its_remaining_quantity_after_a_partial_fill(
        self,
    ) -> None:
        # given a bid of 10 that a sell of 4 from another participant partly fills
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(
            limit_order(
                order_id="s1",
                side=Side.SELL,
                price=99,
                participant_id="p2",
                quantity=4,
            ),
            seq=2,
        )

        # when the state is read
        state = book_state(book)

        # then the bid shows 6 remaining and the next trade id has advanced
        assert state == BookState(
            bids=(
                RestingState(
                    order_id="b1",
                    participant_id="p1",
                    side=Side.BUY,
                    price=99,
                    seq=1,
                    total_quantity=10,
                    remaining=6,
                ),
            ),
            asks=(),
            next_trade_id=2,
        )

    def test_resting_order_reports_its_total_after_an_amend(self) -> None:
        # given a bid of 10 that is amended down to a total of 7
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.amend("b1", price=99, quantity=7, seq=2)

        # when the state is read
        state = book_state(book)

        # then the bid shows a total quantity of 7 and 7 remaining
        assert state.orders["b1"].total_quantity == 7
        assert state.orders["b1"].remaining == 7

    def test_orders_lookup_is_keyed_by_order_id(self) -> None:
        # given a bid and an ask
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        book.submit(limit_order(order_id="s1", side=Side.SELL, price=101), seq=2)

        # when the state is read
        state = book_state(book)

        # then every resting order can be found by id
        assert set(state.orders) == {"b1", "s1"}
        assert state.orders["s1"].price == 101

    def test_state_is_a_snapshot_that_later_changes_do_not_alter(self) -> None:
        # given a state read while one order rests
        book = OrderBook()
        book.submit(limit_order(order_id="b1", side=Side.BUY, price=99), seq=1)
        before = book_state(book)

        # when the order is cancelled afterwards
        book.cancel("b1")

        # then the earlier state still lists it, and the new state does not
        assert set(before.orders) == {"b1"}
        assert book_state(book).orders == {}
