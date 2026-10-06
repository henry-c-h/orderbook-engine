from typing import Any

import pytest
from helpers import assert_book_consistent

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Rejected,
    RejectReason,
    Replaced,
)
from orderbook_engine.domain.models import (
    Order,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from orderbook_engine.engine.order_book import OrderBook

# Matching tests need two different participants: a match between two orders of
# the same participant is a self-trade, which is prevented rather than traded.
P1 = "p1"
P2 = "p2"


def limit(
    order_id: str,
    side: Side,
    price: int,
    *,
    quantity: int = 10,
    participant_id: str = "p1",
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> Order:
    return Order(
        order_id=order_id,
        participant_id=participant_id,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
        time_in_force=time_in_force,
    )


def buy(
    order_id: str,
    price: int,
    *,
    quantity: int = 10,
    participant_id: str = "p1",
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> Order:
    return limit(
        order_id,
        Side.BUY,
        price,
        quantity=quantity,
        participant_id=participant_id,
        time_in_force=time_in_force,
    )


def sell(
    order_id: str,
    price: int,
    *,
    quantity: int = 10,
    participant_id: str = "p1",
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> Order:
    return limit(
        order_id,
        Side.SELL,
        price,
        quantity=quantity,
        participant_id=participant_id,
        time_in_force=time_in_force,
    )


def create_market_order(
    order_id: str,
    side: Side,
    *,
    quantity: int = 10,
    participant_id: str = "p1",
) -> Order:
    return Order(
        order_id=order_id,
        participant_id=participant_id,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        time_in_force=TimeInForce.IOC,
    )


def make_trade(
    trade_id: int, price: int, quantity: int, aggressor: Order, resting: Order
) -> Trade:
    """The Trade we expect when `aggressor` matches against `resting`."""
    return Trade(
        trade_id=trade_id,
        price=price,
        quantity=quantity,
        aggressor_order_id=aggressor.order_id,
        resting_order_id=resting.order_id,
        aggressor_side=aggressor.side,
        aggressor_participant_id=aggressor.participant_id,
        resting_participant_id=resting.participant_id,
    )


def trade_ids(events: list[Event]) -> list[int]:
    return [e.trade_id for e in events if isinstance(e, Trade)]


def assert_not_crossed(book: OrderBook) -> None:
    if book.best_bid is not None and book.best_ask is not None:
        assert book.best_bid < book.best_ask


def assert_no_self_trades(events: list[Event]) -> None:
    """No trade has the same participant on both sides."""
    for event in events:
        if isinstance(event, Trade):
            assert event.aggressor_participant_id != event.resting_participant_id


class TestBestPrices:
    def test_empty_book_has_no_best_prices(self) -> None:
        book = OrderBook()
        assert book.best_bid is None
        assert book.best_ask is None

    def test_resting_a_buy_sets_best_bid(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)
        assert book.best_bid == 100
        assert book.best_ask is None

    def test_resting_a_sell_sets_best_ask(self) -> None:
        book = OrderBook()
        book.submit(sell("s1", 105), seq=1)
        assert book.best_ask == 105
        assert book.best_bid is None

    def test_best_bid_is_the_highest_buy(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 99), seq=1)
        book.submit(buy("b2", 101), seq=2)
        book.submit(buy("b3", 100), seq=3)
        assert book.best_bid == 101

    def test_best_ask_is_the_lowest_sell(self) -> None:
        book = OrderBook()
        book.submit(sell("s1", 105), seq=1)
        book.submit(sell("s2", 103), seq=2)
        book.submit(sell("s3", 104), seq=3)
        assert book.best_ask == 103


class TestSubmitRestingOrders:
    def test_emits_accepted_with_seq_and_order(self) -> None:
        book = OrderBook()
        order = buy("b1", 100)
        assert book.submit(order, seq=7) == [Accepted(seq=7, order=order)]

    def test_same_price_orders_keep_arrival_order(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)
        book.submit(buy("b2", 100), seq=2)
        book.submit(buy("b3", 100), seq=3)
        assert book.orders_at(Side.BUY, 100) == ["b1", "b2", "b3"]

    def test_orders_at_an_unused_price_is_empty(self) -> None:
        assert OrderBook().orders_at(Side.SELL, 100) == []


def assert_no_trace(book: OrderBook, order: Order) -> None:
    """The book holds nothing for `order`, in the id index or at its price level.

    A market order has no price level, so only the id index is checked for it.
    """
    # the id index: a cancel finds nothing (a rejected cancel changes nothing)
    assert book.cancel(order.order_id) == [
        Rejected(order.order_id, RejectReason.UNKNOWN_ORDER)
    ]
    # the price level it would have rested at
    if order.price is not None:
        assert order.order_id not in book.orders_at(order.side, order.price)


class TestIocLimitOrder:
    def test_fully_filled_emits_no_cancelled(self) -> None:
        # given a resting sell of 10 at 100
        book = OrderBook()
        resting = sell("s1", 100, participant_id=P1)
        incoming = buy("b1", 100, participant_id=P2, time_in_force=TimeInForce.IOC)
        book.submit(resting, seq=1)

        # when an IOC buy of 10 at 100 arrives
        events = book.submit(incoming, seq=2)

        # then it trades in full and nothing is cancelled
        assert events == [
            Accepted(seq=2, order=incoming),
            make_trade(1, 100, 10, incoming, resting),
        ]
        assert_no_trace(book, incoming)
        assert_not_crossed(book)

    def test_partial_fill_cancels_the_remainder_and_nothing_rests(self) -> None:
        # given a resting sell of 4 at 100
        book = OrderBook()
        resting = sell("s1", 100, quantity=4, participant_id=P1)
        incoming = buy(
            "b1", 105, quantity=10, participant_id=P2, time_in_force=TimeInForce.IOC
        )
        book.submit(resting, seq=1)

        # when an IOC buy of 10 at 105 arrives
        events = book.submit(incoming, seq=2)

        # then it trades 4, the other 6 are cancelled, and nothing rests
        assert events == [
            Accepted(seq=2, order=incoming),
            make_trade(1, 100, 4, incoming, resting),
            Cancelled("b1", 6, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.orders_at(Side.BUY, 105) == []
        assert_no_trace(book, incoming)

    def test_order_that_does_not_cross_is_cancelled_in_full(self) -> None:
        # given a resting sell of 10 at 105
        book = OrderBook()
        book.submit(sell("s1", 105, participant_id=P1), seq=1)

        # when an IOC buy of 10 at 100 arrives, which does not cross
        incoming = buy("b1", 100, participant_id=P2, time_in_force=TimeInForce.IOC)
        events = book.submit(incoming, seq=2)

        # then it is accepted and cancelled in full, and the book is unchanged
        assert events == [
            Accepted(seq=2, order=incoming),
            Cancelled("b1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_bid is None
        assert book.best_ask == 105
        assert book.orders_at(Side.SELL, 105) == ["s1"]
        assert_no_trace(book, incoming)

    def test_ioc_on_an_empty_book_is_cancelled_in_full(self) -> None:
        # given an empty book
        book = OrderBook()

        # when an IOC sell of 10 at 100 arrives
        incoming = sell("s1", 100, participant_id=P1, time_in_force=TimeInForce.IOC)
        events = book.submit(incoming, seq=1)

        # then it is accepted and cancelled in full, and nothing rests
        assert events == [
            Accepted(seq=1, order=incoming),
            Cancelled("s1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_ask is None
        assert_no_trace(book, incoming)

    def test_buy_stops_at_its_limit_price(self) -> None:
        # given resting sells of 5 at 101, 102 and 103
        book = OrderBook()
        s1 = sell("s1", 101, quantity=5, participant_id=P1)
        s2 = sell("s2", 102, quantity=5, participant_id=P1)
        s3 = sell("s3", 103, quantity=5, participant_id=P1)
        for seq, order in enumerate([s1, s2, s3], start=1):
            book.submit(order, seq=seq)

        # when an IOC buy of 15 at 102 arrives
        incoming = buy(
            "b1", 102, quantity=15, participant_id=P2, time_in_force=TimeInForce.IOC
        )
        events = book.submit(incoming, seq=4)

        # then it trades at 101 and 102, does not trade through to 103,
        # and cancels the other 5
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 101, 5, incoming, s1),
            make_trade(2, 102, 5, incoming, s2),
            Cancelled("b1", 5, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_ask == 103
        assert book.orders_at(Side.SELL, 103) == ["s3"]
        assert book.best_bid is None
        assert_no_trace(book, incoming)

    def test_sell_stops_at_its_limit_price(self) -> None:
        # given resting buys of 5 at 101, 100 and 99
        book = OrderBook()
        b1 = buy("b1", 101, quantity=5, participant_id=P1)
        b2 = buy("b2", 100, quantity=5, participant_id=P1)
        b3 = buy("b3", 99, quantity=5, participant_id=P1)
        for seq, order in enumerate([b1, b2, b3], start=1):
            book.submit(order, seq=seq)

        # when an IOC sell of 15 at 100 arrives
        incoming = sell(
            "s1", 100, quantity=15, participant_id=P2, time_in_force=TimeInForce.IOC
        )
        events = book.submit(incoming, seq=4)

        # then it trades at 101 and 100, does not trade through to 99,
        # and cancels the other 5
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 101, 5, incoming, b1),
            make_trade(2, 100, 5, incoming, b2),
            Cancelled("s1", 5, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_bid == 99
        assert book.best_ask is None
        assert_no_trace(book, incoming)

    def test_duplicate_live_id_is_rejected_before_matching(self) -> None:
        # given a resting sell with id x at 100
        book = OrderBook()
        book.submit(sell("x", 100, participant_id=P1), seq=1)

        # when an IOC buy that reuses id x arrives, where it would cross
        events = book.submit(
            buy("x", 100, participant_id=P2, time_in_force=TimeInForce.IOC), seq=2
        )

        # then it is rejected, with no trade and no cancel, and the book is unchanged
        assert events == [Rejected("x", RejectReason.DUPLICATE_ORDER_ID)]
        assert book.orders_at(Side.SELL, 100) == ["x"]


class TestMarketOrder:
    def test_buy_sweeps_several_ask_levels(self) -> None:
        # given resting sells of 5 at 103, 101 and 102, submitted in that order
        book = OrderBook()
        s1 = sell("s1", 103, quantity=5, participant_id=P1)
        s2 = sell("s2", 101, quantity=5, participant_id=P1)
        s3 = sell("s3", 102, quantity=5, participant_id=P1)
        for seq, order in enumerate([s1, s2, s3], start=1):
            book.submit(order, seq=seq)

        # when a market buy of 15 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=15, participant_id=P2)
        events = book.submit(incoming, seq=4)

        # then it trades at 101, 102, 103, each at the resting price, with no cancel
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 101, 5, incoming, s2),
            make_trade(2, 102, 5, incoming, s3),
            make_trade(3, 103, 5, incoming, s1),
        ]
        assert book.best_ask is None
        assert_no_trace(book, incoming)

    def test_buy_fills_a_level_in_arrival_order(self) -> None:
        # given three resting sells of 5 at 100, in arrival order s1, s2, s3
        book = OrderBook()
        s1 = sell("s1", 100, quantity=5, participant_id=P1)
        s2 = sell("s2", 100, quantity=5, participant_id=P1)
        s3 = sell("s3", 100, quantity=5, participant_id=P1)
        for seq, order in enumerate([s1, s2, s3], start=1):
            book.submit(order, seq=seq)

        # when a market buy of 7 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=7, participant_id=P2)
        events = book.submit(incoming, seq=4)

        # then it fills s1 and 2 of s2, and s2 keeps the front of the queue
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 100, 5, incoming, s1),
            make_trade(2, 100, 2, incoming, s2),
        ]
        assert book.orders_at(Side.SELL, 100) == ["s2", "s3"]

    def test_sell_sweeps_several_bid_levels(self) -> None:
        # given resting buys of 5 at 99, 101 and 100, submitted in that order
        book = OrderBook()
        b1 = buy("b1", 99, quantity=5, participant_id=P1)
        b2 = buy("b2", 101, quantity=5, participant_id=P1)
        b3 = buy("b3", 100, quantity=5, participant_id=P1)
        for seq, order in enumerate([b1, b2, b3], start=1):
            book.submit(order, seq=seq)

        # when a market sell of 15 arrives
        incoming = create_market_order("m1", Side.SELL, quantity=15, participant_id=P2)
        events = book.submit(incoming, seq=4)

        # then it trades at 101, 100, 99, each at the resting price, with no cancel
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 101, 5, incoming, b2),
            make_trade(2, 100, 5, incoming, b3),
            make_trade(3, 99, 5, incoming, b1),
        ]
        assert book.best_bid is None
        assert_no_trace(book, incoming)

    def test_buy_on_a_thin_book_cancels_the_remainder(self) -> None:
        # given resting sells of 4 at 100 and 3 at 200
        book = OrderBook()
        s1 = sell("s1", 100, quantity=4, participant_id=P1)
        s2 = sell("s2", 200, quantity=3, participant_id=P1)
        book.submit(s1, seq=1)
        book.submit(s2, seq=2)

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P2)
        events = book.submit(incoming, seq=3)

        # then it takes all 7 on offer at any price, and cancels the other 3
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 4, incoming, s1),
            make_trade(2, 200, 3, incoming, s2),
            Cancelled("m1", 3, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_ask is None
        assert book.best_bid is None
        assert_no_trace(book, incoming)

    def test_sell_on_a_thin_book_cancels_the_remainder(self) -> None:
        # given a resting buy of 4 at 100
        book = OrderBook()
        b1 = buy("b1", 100, quantity=4, participant_id=P1)
        book.submit(b1, seq=1)

        # when a market sell of 10 arrives
        incoming = create_market_order("m1", Side.SELL, quantity=10, participant_id=P2)
        events = book.submit(incoming, seq=2)

        # then it trades 4 and cancels the other 6
        assert events == [
            Accepted(seq=2, order=incoming),
            make_trade(1, 100, 4, incoming, b1),
            Cancelled("m1", 6, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert_no_trace(book, incoming)

    def test_market_buy_on_an_empty_book_is_cancelled_in_full(self) -> None:
        # given an empty book
        book = OrderBook()

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, participant_id=P1)
        events = book.submit(incoming, seq=1)

        # then it is accepted and cancelled in full, with no trades
        assert events == [
            Accepted(seq=1, order=incoming),
            Cancelled("m1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert_no_trace(book, incoming)

    def test_market_sell_on_an_empty_book_is_cancelled_in_full(self) -> None:
        # given an empty book
        book = OrderBook()

        # when a market sell of 10 arrives
        incoming = create_market_order("m1", Side.SELL, participant_id=P1)
        events = book.submit(incoming, seq=1)

        # then it is accepted and cancelled in full, with no trades
        assert events == [
            Accepted(seq=1, order=incoming),
            Cancelled("m1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert_no_trace(book, incoming)

    def test_market_buy_ignores_the_same_side_of_the_book(self) -> None:
        # given only a resting buy of 10 at 100
        book = OrderBook()
        book.submit(buy("b1", 100, participant_id=P1), seq=1)

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, participant_id=P2)
        events = book.submit(incoming, seq=2)

        # then there is nothing to trade against: it is cancelled and b1 is untouched
        assert events == [
            Accepted(seq=2, order=incoming),
            Cancelled("m1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert book.orders_at(Side.BUY, 100) == ["b1"]

    def test_duplicate_live_id_is_rejected_before_matching(self) -> None:
        # given a resting sell with id x at 100
        book = OrderBook()
        book.submit(sell("x", 100, participant_id=P1), seq=1)

        # when a market buy that reuses id x arrives
        events = book.submit(
            create_market_order("x", Side.BUY, participant_id=P2), seq=2
        )

        # then it is rejected, with no trade and no cancel, and the book is unchanged
        assert events == [Rejected("x", RejectReason.DUPLICATE_ORDER_ID)]
        assert book.orders_at(Side.SELL, 100) == ["x"]


class TestIocAndMarketAcrossCommands:
    def test_ioc_and_market_ids_can_be_submitted_again(self) -> None:
        # given an IOC remainder and a market remainder were both cancelled
        book = OrderBook()
        book.submit(buy("i1", 100, time_in_force=TimeInForce.IOC), seq=1)
        book.submit(create_market_order("m1", Side.SELL), seq=2)

        # when the same ids are submitted again
        again_ioc = buy("i1", 100, time_in_force=TimeInForce.IOC)
        again_market = create_market_order("m1", Side.SELL)
        ioc_events = book.submit(again_ioc, seq=3)
        market_events = book.submit(again_market, seq=4)

        # then neither is rejected as a duplicate: both are accepted, then cancelled
        assert ioc_events == [
            Accepted(seq=3, order=again_ioc),
            Cancelled("i1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert market_events == [
            Accepted(seq=4, order=again_market),
            Cancelled("m1", 10, CancelReason.IOC_REMAINDER),
        ]

    @pytest.mark.parametrize("use_market_order", [False, True])
    def test_events_are_accepted_then_trades_then_cancelled(
        self, use_market_order: bool
    ) -> None:
        # given resting sells of 5 at 100 and 5 at 101
        book = OrderBook()
        book.submit(sell("s1", 100, quantity=5, participant_id=P1), seq=1)
        book.submit(sell("s2", 101, quantity=5, participant_id=P1), seq=2)

        # when an IOC buy or a market buy of 15 arrives, which needs more than the book has
        incoming = (
            create_market_order("m1", Side.BUY, quantity=15, participant_id=P2)
            if use_market_order
            else buy(
                "b1",
                101,
                quantity=15,
                participant_id=P2,
                time_in_force=TimeInForce.IOC,
            )
        )
        events = book.submit(incoming, seq=3)

        # then the events are Accepted, the two Trades, then the Cancelled
        assert [type(e) for e in events] == [Accepted, Trade, Trade, Cancelled]

    def test_trade_ids_count_up_across_gtc_ioc_and_market_commands(self) -> None:
        # given four resting sells of 5 at 100
        book = OrderBook()
        for n in range(1, 5):
            book.submit(sell(f"s{n}", 100, quantity=5, participant_id=P1), seq=n)

        # when a GTC buy, an IOC buy and a market buy each trade
        gtc = book.submit(buy("b1", 100, quantity=5, participant_id=P2), seq=5)
        ioc = book.submit(
            buy(
                "b2", 100, quantity=5, participant_id=P2, time_in_force=TimeInForce.IOC
            ),
            seq=6,
        )
        mkt = book.submit(
            create_market_order("m1", Side.BUY, quantity=10, participant_id=P2), seq=7
        )

        # then trade ids continue 1, 2, 3, 4 across the three commands
        assert trade_ids(gtc) + trade_ids(ioc) + trade_ids(mkt) == [1, 2, 3, 4]
        assert_not_crossed(book)

    def test_invalid_seq_is_rejected_before_the_book_changes(self) -> None:
        # given a resting sell at 100
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)

        # when a market buy arrives with an invalid seq, then it raises
        with pytest.raises(ValueError, match="seq"):
            book.submit(create_market_order("m1", Side.BUY, participant_id=P2), seq=0)

        # then the sell is untouched and the next trade id is 1
        assert book.orders_at(Side.SELL, 100) == ["s1"]
        later = book.submit(
            create_market_order("m2", Side.BUY, participant_id=P2), seq=2
        )
        assert trade_ids(later) == [1]


class TestDuplicateOrderId:
    def test_live_id_is_rejected_and_book_is_unchanged(self) -> None:
        book = OrderBook()
        book.submit(buy("x", 100), seq=1)
        events = book.submit(sell("x", 105), seq=2)
        assert events == [Rejected("x", RejectReason.DUPLICATE_ORDER_ID)]
        assert book.best_bid == 100
        assert book.best_ask is None
        assert book.orders_at(Side.BUY, 100) == ["x"]

    def test_id_is_unique_across_participants(self) -> None:
        book = OrderBook()
        book.submit(buy("x", 100, participant_id="p1"), seq=1)
        events = book.submit(buy("x", 99, participant_id="p2"), seq=2)
        assert events == [Rejected("x", RejectReason.DUPLICATE_ORDER_ID)]


class TestCancel:
    def test_emits_cancelled_with_remaining_quantity(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 100, quantity=7), seq=1)
        assert book.cancel("b1") == [Cancelled("b1", 7, CancelReason.USER_REQUESTED)]

    def test_cancelling_the_last_order_at_a_price_removes_the_level(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)
        book.submit(buy("b2", 99), seq=2)
        book.cancel("b1")
        assert book.best_bid == 99
        assert book.orders_at(Side.BUY, 100) == []

    def test_cancelling_the_only_order_empties_the_side(self) -> None:
        book = OrderBook()
        book.submit(sell("s1", 105), seq=1)
        book.cancel("s1")
        assert book.best_ask is None

    def test_cancelling_one_of_two_keeps_the_level_and_order(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)
        book.submit(buy("b2", 100), seq=2)
        book.submit(buy("b3", 100), seq=3)
        book.cancel("b2")
        assert book.best_bid == 100
        assert book.orders_at(Side.BUY, 100) == ["b1", "b3"]

    def test_unknown_id_is_rejected(self) -> None:
        events = OrderBook().cancel("nope")
        assert events == [Rejected("nope", RejectReason.UNKNOWN_ORDER)]

    def test_already_cancelled_id_is_rejected(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)
        book.cancel("b1")
        assert book.cancel("b1") == [Rejected("b1", RejectReason.UNKNOWN_ORDER)]

    def test_filled_ids_are_rejected(self) -> None:
        # given s1 and b1 have fully traded against each other
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)
        book.submit(buy("b1", 100, participant_id=P2), seq=2)

        # when the filled resting order and the filled aggressor are cancelled
        resting_events = book.cancel("s1")
        aggressor_events = book.cancel("b1")

        # then both are rejected as unknown
        assert resting_events == [Rejected("s1", RejectReason.UNKNOWN_ORDER)]
        assert aggressor_events == [Rejected("b1", RejectReason.UNKNOWN_ORDER)]
        assert_book_consistent(book)

    def test_partly_filled_order_is_cancelled_with_its_remaining_quantity(
        self,
    ) -> None:
        # given a resting sell of 10 at 100 that a buy of 4 has filled
        book = OrderBook()
        book.submit(sell("s1", 100, quantity=10, participant_id=P1), seq=1)
        book.submit(buy("b1", 100, quantity=4, participant_id=P2), seq=2)

        # when the sell is cancelled
        events = book.cancel("s1")

        # then only the 6 that are left are cancelled, and the book is empty
        assert events == [Cancelled("s1", 6, CancelReason.USER_REQUESTED)]
        assert book.best_ask is None
        assert_book_consistent(book)

    def test_cancelled_id_can_be_reused(self) -> None:
        book = OrderBook()
        book.submit(buy("x", 100), seq=1)
        book.cancel("x")
        new_order = sell("x", 105)
        assert book.submit(new_order, seq=2) == [Accepted(seq=2, order=new_order)]
        assert book.best_ask == 105
        assert book.best_bid is None


class TestMatching:
    def test_exact_fill_empties_the_book(self) -> None:
        # given a resting sell of 10 at 100
        book = OrderBook()
        resting = sell("s1", 100, participant_id=P1)
        incoming = buy("b1", 100, participant_id=P2)
        book.submit(resting, seq=1)

        # when a buy of 10 at 100 arrives
        events = book.submit(incoming, seq=2)

        # then one trade fills both orders and the book is empty
        assert events == [
            Accepted(seq=2, order=incoming),
            make_trade(1, 100, 10, incoming, resting),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.orders_at(Side.SELL, 100) == []
        assert book.orders_at(Side.BUY, 100) == []
        assert_not_crossed(book)

    def test_aggressor_remainder_rests_at_its_limit_price(self) -> None:
        # given a resting sell of 4 at 100
        book = OrderBook()
        resting = sell("s1", 100, quantity=4, participant_id=P1)
        incoming = buy("b1", 105, quantity=10, participant_id=P2)
        book.submit(resting, seq=1)

        # when a buy of 10 at 105 arrives
        events = book.submit(incoming, seq=2)

        # then it trades 4 at 100, and its remainder rests at its limit of 105
        assert events == [
            Accepted(seq=2, order=incoming),
            make_trade(1, 100, 4, incoming, resting),
        ]
        assert book.best_bid == 105
        assert book.orders_at(Side.BUY, 105) == ["b1"]
        assert book.orders_at(Side.BUY, 100) == []
        assert book.best_ask is None
        assert_not_crossed(book)

    def test_aggressor_remainder_keeps_its_reduced_quantity(self) -> None:
        # given a buy of 10 at 105 that traded 4 against a sell and rests with 6 left
        book = OrderBook()
        book.submit(sell("s1", 100, quantity=4, participant_id=P1), seq=1)
        first = buy("b1", 105, quantity=10, participant_id=P2)
        book.submit(first, seq=2)

        # when a sell of 10 at 105 arrives
        later = sell("s2", 105, quantity=10, participant_id=P1)
        events = book.submit(later, seq=3)

        # then only 6 trade: b1 has 6 left (10 - 4 filled by s1)
        assert events == [
            Accepted(seq=3, order=later),
            make_trade(2, 105, 6, later, first),
        ]
        assert book.best_bid is None
        assert_not_crossed(book)

    def test_partially_filled_resting_order_keeps_its_place_at_the_front(
        self,
    ) -> None:
        # given two resting sells of 10 at 100, s1 first and s2 behind it
        book = OrderBook()
        s1 = sell("s1", 100, quantity=10, participant_id=P1)
        s2 = sell("s2", 100, quantity=10, participant_id=P1)
        book.submit(s1, seq=1)
        book.submit(s2, seq=2)

        # when a buy of 4 arrives
        first = buy("b1", 100, quantity=4, participant_id=P2)
        first_events = book.submit(first, seq=3)

        # then it trades 4 against s1, which keeps the front of the queue
        assert first_events == [
            Accepted(seq=3, order=first),
            make_trade(1, 100, 4, first, s1),
        ]
        assert book.orders_at(Side.SELL, 100) == ["s1", "s2"]

        # when a buy of 6 arrives
        second = buy("b2", 100, quantity=6, participant_id=P2)
        second_events = book.submit(second, seq=4)

        # then it trades 6 against s1 (6 left), before s2 is touched
        assert second_events == [
            Accepted(seq=4, order=second),
            make_trade(2, 100, 6, second, s1),
        ]
        assert book.orders_at(Side.SELL, 100) == ["s2"]
        assert_not_crossed(book)

    def test_buy_sweeps_asks_lowest_price_first(self) -> None:
        # given resting sells of 5 at 103, 101 and 102, submitted in that order
        book = OrderBook()
        s1 = sell("s1", 103, quantity=5, participant_id=P1)
        s2 = sell("s2", 101, quantity=5, participant_id=P1)
        s3 = sell("s3", 102, quantity=5, participant_id=P1)
        book.submit(s1, seq=1)
        book.submit(s2, seq=2)
        book.submit(s3, seq=3)

        # when a buy of 15 at 103 arrives
        incoming = buy("b1", 103, quantity=15, participant_id=P2)
        events = book.submit(incoming, seq=4)

        # then it trades at 101, 102, 103: lowest price first, and the book is empty
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 101, 5, incoming, s2),
            make_trade(2, 102, 5, incoming, s3),
            make_trade(3, 103, 5, incoming, s1),
        ]
        assert book.best_ask is None
        assert book.best_bid is None
        assert_not_crossed(book)

    def test_sell_sweeps_bids_highest_price_first(self) -> None:
        # given resting buys of 5 at 99, 101 and 100, submitted in that order
        book = OrderBook()
        b1 = buy("b1", 99, quantity=5, participant_id=P1)
        b2 = buy("b2", 101, quantity=5, participant_id=P1)
        b3 = buy("b3", 100, quantity=5, participant_id=P1)
        book.submit(b1, seq=1)
        book.submit(b2, seq=2)
        book.submit(b3, seq=3)

        # when a sell of 15 at 99 arrives
        incoming = sell("s1", 99, quantity=15, participant_id=P2)
        events = book.submit(incoming, seq=4)

        # then it trades at 101, 100, 99: highest price first, and the book is empty
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 101, 5, incoming, b2),
            make_trade(2, 100, 5, incoming, b3),
            make_trade(3, 99, 5, incoming, b1),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert_not_crossed(book)

    def test_sweep_stops_at_the_limit_price_and_rests_the_rest(self) -> None:
        # given resting sells of 5 at 101, 102 and 103
        book = OrderBook()
        s1 = sell("s1", 101, quantity=5, participant_id=P1)
        s2 = sell("s2", 102, quantity=5, participant_id=P1)
        s3 = sell("s3", 103, quantity=5, participant_id=P1)
        for seq, order in enumerate([s1, s2, s3], start=1):
            book.submit(order, seq=seq)

        # when a buy of 15 at 102 arrives
        incoming = buy("b1", 102, quantity=15, participant_id=P2)
        events = book.submit(incoming, seq=4)

        # then it trades at 101 and 102 only, and its remainder rests at 102
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 101, 5, incoming, s1),
            make_trade(2, 102, 5, incoming, s2),
        ]
        assert book.best_bid == 102
        assert book.best_ask == 103  # s3 is out of reach and untouched
        assert_not_crossed(book)

    def test_orders_at_one_level_fill_in_arrival_order(self) -> None:
        # given three resting sells of 5 at 100, in arrival order s1, s2, s3
        book = OrderBook()
        s1 = sell("s1", 100, quantity=5, participant_id=P1)
        s2 = sell("s2", 100, quantity=5, participant_id=P1)
        s3 = sell("s3", 100, quantity=5, participant_id=P1)
        for seq, order in enumerate([s1, s2, s3], start=1):
            book.submit(order, seq=seq)

        # when a buy of 10 at 100 arrives
        incoming = buy("b1", 100, quantity=10, participant_id=P2)
        events = book.submit(incoming, seq=4)

        # then it fills s1 and then s2, and only s3 is left
        assert events == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 100, 5, incoming, s1),
            make_trade(2, 100, 5, incoming, s2),
        ]
        assert book.orders_at(Side.SELL, 100) == ["s3"]
        assert_not_crossed(book)

    def test_buy_trades_at_the_resting_asks_price(self) -> None:
        # given a resting sell at 100
        book = OrderBook()
        resting = sell("s1", 100, participant_id=P1)
        incoming = buy("b1", 105, participant_id=P2)
        book.submit(resting, seq=1)

        # when a buy at 105 arrives
        events = book.submit(incoming, seq=2)

        # then the trade price is 100 (the resting price), not 105
        # and the book is not crossed
        assert events[1] == make_trade(1, 100, 10, incoming, resting)
        assert_not_crossed(book)

    def test_sell_trades_at_the_resting_bids_price(self) -> None:
        # given a resting buy at 100
        book = OrderBook()
        resting = buy("b1", 100, participant_id=P1)
        incoming = sell("s1", 95, participant_id=P2)
        book.submit(resting, seq=1)

        # when a sell at 95 arrives
        events = book.submit(incoming, seq=2)

        # then the trade price is 100 (the resting price), not 95
        # and the book is not crossed
        assert events[1] == make_trade(1, 100, 10, incoming, resting)
        assert_not_crossed(book)

    def test_trade_ids_count_up_across_commands(self) -> None:
        # given three resting sells of 5 at 100
        book = OrderBook()
        for n in range(1, 4):
            book.submit(sell(f"s{n}", 100, quantity=5, participant_id=P1), seq=n)

        # when a buy of 10 (two trades) and then a buy of 5 (one trade) arrive
        first = book.submit(buy("b1", 100, quantity=10, participant_id=P2), seq=4)
        second = book.submit(buy("b2", 100, quantity=5, participant_id=P2), seq=5)

        # then trade ids continue 1, 2, 3 across the two commands
        # and the book is not crossed
        assert trade_ids(first) + trade_ids(second) == [1, 2, 3]
        assert_not_crossed(book)

    def test_events_are_accepted_then_trades(self) -> None:
        # given resting sells of 5 at 100 and 5 at 101
        book = OrderBook()
        book.submit(sell("s1", 100, quantity=5, participant_id=P1), seq=1)
        book.submit(sell("s2", 101, quantity=5, participant_id=P1), seq=2)

        # when a buy of 10 at 101 arrives
        events = book.submit(buy("b1", 101, quantity=10, participant_id=P2), seq=3)

        # then the events are Accepted first, then the two Trades
        # and the book is not crossed
        assert [type(e) for e in events] == [Accepted, Trade, Trade]
        assert_not_crossed(book)

    def test_filled_order_ids_can_be_reused(self) -> None:
        # given s1 and b1 have fully traded against each other
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)
        book.submit(buy("b1", 100, participant_id=P2), seq=2)

        # when new orders reuse the ids s1 (a filled resting) and b1 (a filled aggressor)
        new_sell = sell("s1", 100, participant_id=P1)
        new_buy = buy("b1", 99, participant_id=P2)
        sell_events = book.submit(new_sell, seq=3)
        buy_events = book.submit(new_buy, seq=4)

        # then both are accepted and rest
        assert sell_events == [Accepted(seq=3, order=new_sell)]
        assert buy_events == [Accepted(seq=4, order=new_buy)]
        assert book.best_ask == 100
        assert book.best_bid == 99
        assert_not_crossed(book)

    def test_duplicate_live_id_is_rejected_without_matching(self) -> None:
        # given a resting sell with id x at 101
        book = OrderBook()
        book.submit(sell("x", 101, participant_id=P1), seq=1)

        # when a buy that reuses id x arrives at 105, where it would cross
        events = book.submit(buy("x", 105, participant_id=P2), seq=2)

        # then it is rejected and the book is unchanged
        assert events == [Rejected("x", RejectReason.DUPLICATE_ORDER_ID)]
        assert book.best_ask == 101
        assert book.orders_at(Side.SELL, 101) == ["x"]
        assert book.best_bid is None

        # when a valid buy then trades against x
        later = book.submit(buy("b2", 101, participant_id=P2), seq=3)

        # then its trade id is 1: the rejected command did not use one
        # and the book is not crossed
        assert trade_ids(later) == [1]
        assert_not_crossed(book)

    def test_invalid_seq_is_rejected_before_the_book_changes(self) -> None:
        # given a resting sell at 100
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)

        # when a crossing buy arrives with an invalid seq, then it raises
        with pytest.raises(ValueError, match="seq"):
            book.submit(buy("b1", 100, participant_id=P2), seq=0)

        # then the sell is untouched: it did not trade
        assert book.orders_at(Side.SELL, 100) == ["s1"]

        # when a valid crossing buy then arrives
        later = book.submit(buy("b2", 100, participant_id=P2), seq=2)

        # then its trade id is 1: the failed command did not use one
        # and the book is not crossed
        assert trade_ids(later) == [1]
        assert_not_crossed(book)

    def test_non_crossing_order_after_trading_still_rests(self) -> None:
        # given s1 and b1 have fully traded, leaving an empty book
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)
        book.submit(buy("b1", 100, participant_id=P2), seq=2)

        # when a buy at 99 and a sell at 105 arrive
        book.submit(buy("b2", 99, participant_id=P2), seq=3)
        book.submit(sell("s2", 105, participant_id=P1), seq=4)

        # then both rest, since neither crosses
        assert book.best_bid == 99
        assert book.best_ask == 105
        assert_not_crossed(book)

    def test_buy_one_tick_below_the_best_ask_rests(self) -> None:
        # given a resting sell of 10 at 100
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)

        # when a GTC buy at 99 arrives, one tick below the ask
        incoming = buy("b1", 99, participant_id=P2)
        events = book.submit(incoming, seq=2)

        # then it does not trade, and it rests
        assert events == [Accepted(seq=2, order=incoming)]
        assert book.orders_at(Side.BUY, 99) == ["b1"]
        assert book.orders_at(Side.SELL, 100) == ["s1"]
        assert_book_consistent(book)

    def test_sell_one_tick_above_the_best_bid_rests(self) -> None:
        # given a resting buy of 10 at 100
        book = OrderBook()
        book.submit(buy("b1", 100, participant_id=P1), seq=1)

        # when a GTC sell at 101 arrives, one tick above the bid
        incoming = sell("s1", 101, participant_id=P2)
        events = book.submit(incoming, seq=2)

        # then it does not trade, and it rests
        assert events == [Accepted(seq=2, order=incoming)]
        assert book.orders_at(Side.SELL, 101) == ["s1"]
        assert book.orders_at(Side.BUY, 100) == ["b1"]
        assert_book_consistent(book)

    def test_ioc_buy_one_tick_below_the_best_ask_is_cancelled_untraded(self) -> None:
        # given a resting sell of 10 at 100
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)

        # when an IOC buy at 99 arrives, one tick below the ask
        incoming = buy("b1", 99, participant_id=P2, time_in_force=TimeInForce.IOC)
        events = book.submit(incoming, seq=2)

        # then it does not trade, and it is cancelled in full
        assert events == [
            Accepted(seq=2, order=incoming),
            Cancelled("b1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert book.orders_at(Side.SELL, 100) == ["s1"]
        assert_book_consistent(book)

    def test_ioc_sell_one_tick_above_the_best_bid_is_cancelled_untraded(self) -> None:
        # given a resting buy of 10 at 100
        book = OrderBook()
        book.submit(buy("b1", 100, participant_id=P1), seq=1)

        # when an IOC sell at 101 arrives, one tick above the bid
        incoming = sell("s1", 101, participant_id=P2, time_in_force=TimeInForce.IOC)
        events = book.submit(incoming, seq=2)

        # then it does not trade, and it is cancelled in full
        assert events == [
            Accepted(seq=2, order=incoming),
            Cancelled("s1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert book.orders_at(Side.BUY, 100) == ["b1"]
        assert_book_consistent(book)


class TestSelfTradePrevention:
    def test_self_trade_with_no_prior_trades_cancels_the_whole_order(self) -> None:
        # given a resting sell of 10 at 100 from p1
        book = OrderBook()
        own = sell("s1", 100, participant_id=P1)
        book.submit(own, seq=1)

        # when a buy of 10 at 100 from p1 arrives
        incoming = buy("b1", 100, participant_id=P1)
        events = book.submit(incoming, seq=2)

        # then it is accepted and cancelled in full, with no trade
        assert events == [
            Accepted(seq=2, order=incoming),
            Cancelled("b1", 10, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert book.orders_at(Side.SELL, 100) == ["s1"]
        assert book.best_bid is None
        assert_no_trace(book, incoming)

        # when a buy from p2 then arrives
        later = buy("b2", 100, participant_id=P2)

        # then the resting sell is untouched: it trades all 10, as trade 1
        assert book.submit(later, seq=3) == [
            Accepted(seq=3, order=later),
            make_trade(1, 100, 10, later, own),
        ]

    def test_trades_made_before_the_self_trade_stand(self) -> None:
        # given resting sells of 4 from p2 and then 10 from p1, both at 100
        book = OrderBook()
        other = sell("s1", 100, quantity=4, participant_id=P2)
        own = sell("s2", 100, quantity=10, participant_id=P1)
        book.submit(other, seq=1)
        book.submit(own, seq=2)

        # when a buy of 10 at 100 from p1 arrives
        incoming = buy("b1", 100, quantity=10, participant_id=P1)
        events = book.submit(incoming, seq=3)

        # then it trades 4 against p2, then the other 6 are cancelled
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 4, incoming, other),
            Cancelled("b1", 6, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert book.orders_at(Side.SELL, 100) == ["s2"]
        assert_no_trace(book, incoming)

    def test_self_trade_at_a_worse_price_after_trading_a_better_one(self) -> None:
        # given a resting sell of 5 at 100 from p2 and of 5 at 101 from p1
        book = OrderBook()
        other = sell("s1", 100, quantity=5, participant_id=P2)
        own = sell("s2", 101, quantity=5, participant_id=P1)
        book.submit(other, seq=1)
        book.submit(own, seq=2)

        # when a buy of 15 at 101 from p1 arrives
        incoming = buy("b1", 101, quantity=15, participant_id=P1)
        events = book.submit(incoming, seq=3)

        # then it trades 5 at 100, and the other 10 are cancelled at the self-trade
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 5, incoming, other),
            Cancelled("b1", 10, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert book.best_ask == 101
        assert book.orders_at(Side.SELL, 101) == ["s2"]
        assert book.best_bid is None

    def test_gtc_remainder_is_cancelled_and_does_not_rest(self) -> None:
        # given resting sells of 4 from p2 and then 10 from p1, both at 100
        book = OrderBook()
        other = sell("s1", 100, quantity=4, participant_id=P2)
        book.submit(other, seq=1)
        book.submit(sell("s2", 100, quantity=10, participant_id=P1), seq=2)

        # when a GTC buy of 10 at 105 from p1 arrives, with a limit above the book
        incoming = buy("b1", 105, quantity=10, participant_id=P1)
        events = book.submit(incoming, seq=3)

        # then it trades 4, and the 6 left are cancelled rather than rested at 105
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 4, incoming, other),
            Cancelled("b1", 6, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert book.best_bid is None
        assert_no_trace(book, incoming)

    def test_ioc_reason_is_self_trade_prevented(self) -> None:
        # given resting sells of 4 from p2 and then 10 from p1, both at 100
        book = OrderBook()
        other = sell("s1", 100, quantity=4, participant_id=P2)
        book.submit(other, seq=1)
        book.submit(sell("s2", 100, quantity=10, participant_id=P1), seq=2)

        # when an IOC buy of 10 at 100 from p1 arrives
        incoming = buy(
            "b1", 100, quantity=10, participant_id=P1, time_in_force=TimeInForce.IOC
        )
        events = book.submit(incoming, seq=3)

        # then there is exactly one Cancelled, with the self-trade reason
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 4, incoming, other),
            Cancelled("b1", 6, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert_no_trace(book, incoming)

    def test_market_reason_is_self_trade_prevented(self) -> None:
        # given resting sells of 4 from p2 and then 10 from p1, both at 100
        book = OrderBook()
        other = sell("s1", 100, quantity=4, participant_id=P2)
        book.submit(other, seq=1)
        book.submit(sell("s2", 100, quantity=10, participant_id=P1), seq=2)

        # when a market buy of 10 from p1 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P1)
        events = book.submit(incoming, seq=3)

        # then there is exactly one Cancelled, with the self-trade reason
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 4, incoming, other),
            Cancelled("m1", 6, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert_no_trace(book, incoming)

    def test_sell_side_self_trade_mirrors_the_buy_side(self) -> None:
        # given resting buys of 4 from p2 and then 10 from p1, both at 100
        book = OrderBook()
        other = buy("b1", 100, quantity=4, participant_id=P2)
        book.submit(other, seq=1)
        book.submit(buy("b2", 100, quantity=10, participant_id=P1), seq=2)

        # when a GTC sell of 10 at 95 from p1 arrives
        incoming = sell("s1", 95, quantity=10, participant_id=P1)
        events = book.submit(incoming, seq=3)

        # then it trades 4, cancels the other 6, and does not rest at 95
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 4, incoming, other),
            Cancelled("s1", 6, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert book.best_ask is None
        assert book.orders_at(Side.BUY, 100) == ["b2"]
        assert_no_trace(book, incoming)

    def test_matching_stops_at_the_first_self_trade(self) -> None:
        # given resting sells at 100 from p1 and then from p2 behind it
        book = OrderBook()
        own = sell("s1", 100, quantity=5, participant_id=P1)
        other = sell("s2", 100, quantity=5, participant_id=P2)
        book.submit(own, seq=1)
        book.submit(other, seq=2)

        # when a buy of 5 at 100 from p1 arrives
        incoming = buy("b1", 100, quantity=5, participant_id=P1)
        events = book.submit(incoming, seq=3)

        # then it does not skip past s1 to trade with s2: it is cancelled in full
        assert events == [
            Accepted(seq=3, order=incoming),
            Cancelled("b1", 5, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert book.orders_at(Side.SELL, 100) == ["s1", "s2"]

    def test_own_order_behind_the_fill_is_never_reached(self) -> None:
        # given resting sells at 100 from p2 and then from p1 behind it
        book = OrderBook()
        other = sell("s1", 100, quantity=10, participant_id=P2)
        own = sell("s2", 100, quantity=10, participant_id=P1)
        book.submit(other, seq=1)
        book.submit(own, seq=2)

        # when a buy of 10 at 100 from p1 arrives
        incoming = buy("b1", 100, quantity=10, participant_id=P1)
        events = book.submit(incoming, seq=3)

        # then it fills against p2 in full, with no Cancelled
        assert events == [
            Accepted(seq=3, order=incoming),
            make_trade(1, 100, 10, incoming, other),
        ]
        assert_no_self_trades(events)
        assert book.orders_at(Side.SELL, 100) == ["s2"]
        assert_no_trace(book, incoming)

    def test_own_order_that_does_not_cross_is_not_a_self_trade(self) -> None:
        # given a resting sell at 105 from p1
        book = OrderBook()
        book.submit(sell("s1", 105, participant_id=P1), seq=1)

        # when a GTC buy at 100 from p1 arrives, which does not cross
        incoming = buy("b1", 100, participant_id=P1)
        events = book.submit(incoming, seq=2)

        # then it rests as normal
        assert events == [Accepted(seq=2, order=incoming)]
        assert_no_self_trades(events)
        assert book.orders_at(Side.BUY, 100) == ["b1"]
        assert book.best_ask == 105
        assert_not_crossed(book)

    def test_ioc_that_does_not_cross_own_order_keeps_the_ioc_reason(self) -> None:
        # given a resting sell at 105 from p1
        book = OrderBook()
        book.submit(sell("s1", 105, participant_id=P1), seq=1)

        # when an IOC buy at 100 from p1 arrives, which does not cross
        incoming = buy("b1", 100, participant_id=P1, time_in_force=TimeInForce.IOC)
        events = book.submit(incoming, seq=2)

        # then it is cancelled as an IOC remainder, not as a self-trade
        assert events == [
            Accepted(seq=2, order=incoming),
            Cancelled("b1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert_no_self_trades(events)

    def test_resting_order_is_still_live_and_cancellable(self) -> None:
        # given a buy from p1 that was cancelled by hitting p1's own resting sell
        book = OrderBook()
        book.submit(sell("s1", 100, quantity=7, participant_id=P1), seq=1)
        book.submit(buy("b1", 100, participant_id=P1), seq=2)

        # when the resting sell is cancelled by id
        events = book.cancel("s1")

        # then it is cancelled with its full quantity, and the level is gone
        assert events == [Cancelled("s1", 7, CancelReason.USER_REQUESTED)]
        assert book.best_ask is None


class TestAmendRejections:
    def test_unknown_id_is_rejected(self) -> None:
        # given an empty book
        book = OrderBook()

        # when an unknown id is amended
        events = book.amend("nope", price=100, quantity=10, seq=1)

        # then it is rejected
        assert events == [Rejected("nope", RejectReason.UNKNOWN_ORDER)]
        assert_book_consistent(book)

    def test_filled_ids_are_rejected(self) -> None:
        # given s1 and b1 have fully traded against each other
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)
        book.submit(buy("b1", 100, participant_id=P2), seq=2)

        # when the filled resting order and the filled aggressor are amended
        resting_events = book.amend("s1", price=101, quantity=10, seq=3)
        aggressor_events = book.amend("b1", price=99, quantity=10, seq=4)

        # then both are rejected as unknown
        assert resting_events == [Rejected("s1", RejectReason.UNKNOWN_ORDER)]
        assert aggressor_events == [Rejected("b1", RejectReason.UNKNOWN_ORDER)]
        assert_book_consistent(book)

    def test_cancelled_id_is_rejected(self) -> None:
        # given a resting buy that was cancelled
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)
        book.cancel("b1")

        # when it is amended
        events = book.amend("b1", price=100, quantity=5, seq=2)

        # then it is rejected as unknown
        assert events == [Rejected("b1", RejectReason.UNKNOWN_ORDER)]
        assert_book_consistent(book)

    @pytest.mark.parametrize("new_total", [4, 3])
    def test_total_not_above_filled_is_rejected_and_order_untouched(
        self, new_total: int
    ) -> None:
        # given a resting sell of 10 at 100 that has filled 4 and has 6 left
        book = OrderBook()
        resting = sell("s1", 100, quantity=10, participant_id=P1)
        book.submit(resting, seq=1)
        book.submit(buy("b1", 100, quantity=4, participant_id=P2), seq=2)

        # when it is amended to a total of 4 (equal to filled) or 3 (below filled)
        events = book.amend("s1", price=100, quantity=new_total, seq=3)

        # then it is rejected and the order is untouched
        assert events == [Rejected("s1", RejectReason.QUANTITY_BELOW_FILLED)]
        assert book.orders_at(Side.SELL, 100) == ["s1"]
        assert_book_consistent(book)

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P2)
        fills = book.submit(incoming, seq=4)

        # then s1 still has its 6 left
        assert fills == [
            Accepted(seq=4, order=incoming),
            make_trade(2, 100, 6, incoming, resting),
            Cancelled("m1", 4, CancelReason.IOC_REMAINDER),
        ]
        assert_book_consistent(book)

    @pytest.mark.parametrize(
        ("price", "quantity"),
        [(0, 10), (-1, 10), (100, 0), (100, -5), (1.5, 10), (100, 2.5), (100, True)],
    )
    def test_invalid_price_or_quantity_raises_and_book_is_untouched(
        self, price: Any, quantity: Any
    ) -> None:
        # given a resting buy
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)

        # when it is amended with a price or quantity that is not a positive int
        # then it raises
        with pytest.raises(ValueError, match="price|quantity"):
            book.amend("b1", price=price, quantity=quantity, seq=2)

        # and the order is untouched
        assert book.orders_at(Side.BUY, 100) == ["b1"]
        assert_book_consistent(book)

    def test_invalid_seq_raises_before_the_book_changes(self) -> None:
        # given a resting sell of 5 at 101 and a resting buy of 10 at 100
        book = OrderBook()
        book.submit(sell("s1", 101, quantity=5, participant_id=P1), seq=1)
        book.submit(buy("b1", 100, participant_id=P2), seq=2)

        # when the buy is amended to cross, with an invalid seq, then it raises
        with pytest.raises(ValueError, match="seq"):
            book.amend("b1", price=101, quantity=10, seq=0)

        # then nothing changed
        assert book.orders_at(Side.SELL, 101) == ["s1"]
        assert book.orders_at(Side.BUY, 100) == ["b1"]
        assert_book_consistent(book)

        # when the same amend is made with a valid seq
        events = book.amend("b1", price=101, quantity=10, seq=3)

        # then its trade id is 1: the failed amend did not use one
        assert trade_ids(events) == [1]
        assert_book_consistent(book)


class TestAmendKeepingPriority:
    def test_decrease_at_the_same_price_keeps_its_place_and_seq(self) -> None:
        # given three resting sells of 10 at 100, with seq 1, 2 and 3
        book = OrderBook()
        s1 = sell("s1", 100, participant_id=P1)
        s2 = sell("s2", 100, participant_id=P1)
        s3 = sell("s3", 100, participant_id=P1)
        for seq, order in enumerate([s1, s2, s3], start=1):
            book.submit(order, seq=seq)

        # when the first is reduced to a total of 6 at the same price
        events = book.amend("s1", price=100, quantity=6, seq=4)

        # then only Replaced is emitted, with the original seq 1, and s1 stays first
        assert events == [Replaced("s1", seq=1, new_price=100, new_total_quantity=6)]
        assert book.orders_at(Side.SELL, 100) == ["s1", "s2", "s3"]
        assert_book_consistent(book)

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P2)
        fills = book.submit(incoming, seq=5)

        # then s1 fills first, with its reduced 6, and then 4 of s2
        assert fills == [
            Accepted(seq=5, order=incoming),
            make_trade(1, 100, 6, incoming, s1),
            make_trade(2, 100, 4, incoming, s2),
        ]
        assert book.orders_at(Side.SELL, 100) == ["s2", "s3"]
        assert_book_consistent(book)

    def test_decrease_on_a_partially_filled_order_keeps_total_minus_filled(
        self,
    ) -> None:
        # given a resting sell of 10 at 100 that has filled 4 and has 6 left
        book = OrderBook()
        resting = sell("s1", 100, quantity=10, participant_id=P1)
        book.submit(resting, seq=1)
        book.submit(buy("b1", 100, quantity=4, participant_id=P2), seq=2)

        # when its total is reduced to 8
        events = book.amend("s1", price=100, quantity=8, seq=3)

        # then only Replaced is emitted, with the original seq
        assert events == [Replaced("s1", seq=1, new_price=100, new_total_quantity=8)]
        assert_book_consistent(book)

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P2)
        fills = book.submit(incoming, seq=4)

        # then s1 has 4 left (8 total - 4 filled), and the other 6 are cancelled
        assert fills == [
            Accepted(seq=4, order=incoming),
            make_trade(2, 100, 4, incoming, resting),
            Cancelled("m1", 6, CancelReason.IOC_REMAINDER),
        ]
        assert_book_consistent(book)


class TestAmendLosingPriority:
    def test_increase_at_the_same_price_goes_to_the_back_with_a_new_seq(self) -> None:
        # given two resting sells of 10 at 100, s1 first
        book = OrderBook()
        s1 = sell("s1", 100, participant_id=P1)
        s2 = sell("s2", 100, participant_id=P1)
        book.submit(s1, seq=1)
        book.submit(s2, seq=2)

        # when s1 is increased to a total of 15 at the same price
        events = book.amend("s1", price=100, quantity=15, seq=3)

        # then only Replaced is emitted, with the new seq 3, and s1 is now last
        assert events == [Replaced("s1", seq=3, new_price=100, new_total_quantity=15)]
        assert book.orders_at(Side.SELL, 100) == ["s2", "s1"]
        assert_book_consistent(book)

        # when a market buy of 25 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=25, participant_id=P2)
        fills = book.submit(incoming, seq=4)

        # then s2 fills first, then s1 with its new total of 15
        assert fills == [
            Accepted(seq=4, order=incoming),
            make_trade(1, 100, 10, incoming, s2),
            make_trade(2, 100, 15, incoming, s1),
        ]
        assert_book_consistent(book)

    def test_equal_total_at_the_same_price_also_goes_to_the_back(self) -> None:
        # given two resting sells of 10 at 100, s1 first
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)
        book.submit(sell("s2", 100, participant_id=P1), seq=2)

        # when s1 is amended to the same price and the same total
        events = book.amend("s1", price=100, quantity=10, seq=3)

        # then it takes the normal replace path: a new seq, and s1 is now last
        assert events == [Replaced("s1", seq=3, new_price=100, new_total_quantity=10)]
        assert book.orders_at(Side.SELL, 100) == ["s2", "s1"]
        assert_book_consistent(book)

    def test_price_change_to_a_non_crossing_price_moves_the_order(self) -> None:
        # given resting buys of 10 at 100 and at 99
        book = OrderBook()
        book.submit(buy("b1", 100, participant_id=P1), seq=1)
        book.submit(buy("b2", 99, participant_id=P1), seq=2)

        # when b1 is amended to a price of 98
        events = book.amend("b1", price=98, quantity=10, seq=3)

        # then only Replaced is emitted, b1 moves to 98 and the empty level 100 is gone
        assert events == [Replaced("b1", seq=3, new_price=98, new_total_quantity=10)]
        assert book.best_bid == 99
        assert book.orders_at(Side.BUY, 100) == []
        assert book.orders_at(Side.BUY, 98) == ["b1"]
        assert_book_consistent(book)

    def test_price_change_leaves_the_other_orders_at_the_old_level(self) -> None:
        # given two resting buys of 10 at 100
        book = OrderBook()
        book.submit(buy("b1", 100, participant_id=P1), seq=1)
        book.submit(buy("b2", 100, participant_id=P1), seq=2)

        # when b1 is amended to a price of 98
        book.amend("b1", price=98, quantity=10, seq=3)

        # then b2 stays at 100, which is still the best bid
        assert book.best_bid == 100
        assert book.orders_at(Side.BUY, 100) == ["b2"]
        assert book.orders_at(Side.BUY, 98) == ["b1"]
        assert_book_consistent(book)

    def test_price_change_that_crosses_trades_and_rests_the_remainder(self) -> None:
        # given resting sells of 5 at 101 and 5 at 102, and a resting buy of 20 at 100
        book = OrderBook()
        s1 = sell("s1", 101, quantity=5, participant_id=P1)
        s2 = sell("s2", 102, quantity=5, participant_id=P1)
        b1 = buy("b1", 100, quantity=20, participant_id=P2)
        book.submit(s1, seq=1)
        book.submit(s2, seq=2)
        book.submit(b1, seq=3)

        # when b1 is amended to a price of 102
        events = book.amend("b1", price=102, quantity=20, seq=4)

        # then Replaced comes first, then trades at the resting prices 101 and 102
        assert events == [
            Replaced("b1", seq=4, new_price=102, new_total_quantity=20),
            make_trade(1, 101, 5, b1, s1),
            make_trade(2, 102, 5, b1, s2),
        ]
        # and the other 10 rest at the new price, with the old level gone
        assert book.best_bid == 102
        assert book.orders_at(Side.BUY, 102) == ["b1"]
        assert book.orders_at(Side.BUY, 100) == []
        assert book.best_ask is None
        assert_book_consistent(book)

        # when a market sell of 20 from p1 arrives
        incoming = create_market_order("m1", Side.SELL, quantity=20, participant_id=P1)
        fills = book.submit(incoming, seq=5)

        # then b1 has exactly 10 left, and the other 10 are cancelled
        assert fills == [
            Accepted(seq=5, order=incoming),
            make_trade(3, 102, 10, incoming, b1),
            Cancelled("m1", 10, CancelReason.IOC_REMAINDER),
        ]
        assert_book_consistent(book)

    def test_amending_a_sell_across_the_bids_trades_at_the_bids_price(self) -> None:
        # given a resting buy of 10 at 100 and a resting sell of 10 at 105
        book = OrderBook()
        b1 = buy("b1", 100, participant_id=P2)
        s1 = sell("s1", 105, participant_id=P1)
        book.submit(b1, seq=1)
        book.submit(s1, seq=2)

        # when the sell is amended to a price of 95
        events = book.amend("s1", price=95, quantity=10, seq=3)

        # then it trades at the resting bid's price of 100, not at 95
        assert events == [
            Replaced("s1", seq=3, new_price=95, new_total_quantity=10),
            make_trade(1, 100, 10, s1, b1),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert_book_consistent(book)

    def test_amending_a_partly_filled_order_rests_total_minus_filled(self) -> None:
        # given a resting buy of 10 at 100 that a sell of 4 filled, so 6 are left
        book = OrderBook()
        b1 = buy("b1", 100, quantity=10, participant_id=P2)
        book.submit(b1, seq=1)
        book.submit(sell("s1", 100, quantity=4, participant_id=P1), seq=2)
        # and a resting sell of 5 at 101
        s2 = sell("s2", 101, quantity=5, participant_id=P1)
        book.submit(s2, seq=3)

        # when b1 is amended to a price of 101 and a total of 20
        events = book.amend("b1", price=101, quantity=20, seq=4)

        # then it trades 5, and 11 rest (20 total - 4 filled - 5 traded now)
        assert events == [
            Replaced("b1", seq=4, new_price=101, new_total_quantity=20),
            make_trade(2, 101, 5, b1, s2),
        ]
        assert book.orders_at(Side.BUY, 101) == ["b1"]
        assert_book_consistent(book)

        # when a market sell of 20 from p1 arrives
        incoming = create_market_order("m1", Side.SELL, quantity=20, participant_id=P1)
        fills = book.submit(incoming, seq=5)

        # then b1 has exactly 11 left
        assert fills == [
            Accepted(seq=5, order=incoming),
            make_trade(3, 101, 11, incoming, b1),
            Cancelled("m1", 9, CancelReason.IOC_REMAINDER),
        ]
        assert_book_consistent(book)

    def test_amend_that_fully_fills_leaves_nothing_behind(self) -> None:
        # given a resting sell of 10 at 100 and a resting buy of 10 at 99
        book = OrderBook()
        s1 = sell("s1", 100, participant_id=P1)
        b1 = buy("b1", 99, participant_id=P2)
        book.submit(s1, seq=1)
        book.submit(b1, seq=2)

        # when the buy is amended to a price of 100
        events = book.amend("b1", price=100, quantity=10, seq=3)

        # then it fills in full, and neither the level nor the id remains
        assert events == [
            Replaced("b1", seq=3, new_price=100, new_total_quantity=10),
            make_trade(1, 100, 10, b1, s1),
        ]
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.orders_at(Side.BUY, 100) == []
        assert_no_trace(book, b1)
        assert_book_consistent(book)


class TestRepeatedAmends:
    def test_decreases_are_measured_against_the_filled_quantity(self) -> None:
        # given a resting sell of 10 at 100 that has filled 4
        book = OrderBook()
        resting = sell("s1", 100, quantity=10, participant_id=P1)
        book.submit(resting, seq=1)
        book.submit(buy("b1", 100, quantity=4, participant_id=P2), seq=2)

        # when it is decreased to a total of 8, and then to a total equal to filled
        first = book.amend("s1", price=100, quantity=8, seq=3)
        at_filled = book.amend("s1", price=100, quantity=4, seq=4)

        # then the first keeps priority and the second is rejected
        assert first == [Replaced("s1", seq=1, new_price=100, new_total_quantity=8)]
        assert at_filled == [Rejected("s1", RejectReason.QUANTITY_BELOW_FILLED)]
        assert_book_consistent(book)

        # when it is decreased again, to a total of 5
        second = book.amend("s1", price=100, quantity=5, seq=5)

        # then it keeps priority again
        assert second == [Replaced("s1", seq=1, new_price=100, new_total_quantity=5)]

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P2)
        fills = book.submit(incoming, seq=6)

        # then s1 has 1 left (5 total - 4 filled)
        assert fills == [
            Accepted(seq=6, order=incoming),
            make_trade(2, 100, 1, incoming, resting),
            Cancelled("m1", 9, CancelReason.IOC_REMAINDER),
        ]
        assert_book_consistent(book)

    def test_two_decreases_keep_the_original_place(self) -> None:
        # given three resting sells of 10 at 100
        book = OrderBook()
        s1 = sell("s1", 100, participant_id=P1)
        s2 = sell("s2", 100, participant_id=P1)
        s3 = sell("s3", 100, participant_id=P1)
        for seq, order in enumerate([s1, s2, s3], start=1):
            book.submit(order, seq=seq)

        # when the first is decreased twice
        first = book.amend("s1", price=100, quantity=8, seq=4)
        second = book.amend("s1", price=100, quantity=6, seq=5)

        # then both carry the original seq, and s1 is still first
        assert first == [Replaced("s1", seq=1, new_price=100, new_total_quantity=8)]
        assert second == [Replaced("s1", seq=1, new_price=100, new_total_quantity=6)]
        assert book.orders_at(Side.SELL, 100) == ["s1", "s2", "s3"]
        assert_book_consistent(book)

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P2)
        fills = book.submit(incoming, seq=6)

        # then s1 fills with 6, and s2 with 4
        assert fills == [
            Accepted(seq=6, order=incoming),
            make_trade(1, 100, 6, incoming, s1),
            make_trade(2, 100, 4, incoming, s2),
        ]
        assert_book_consistent(book)

    def test_increase_after_a_decrease_is_measured_against_the_decreased_total(
        self,
    ) -> None:
        # given two resting sells of 10 at 100, s1 first
        book = OrderBook()
        s1 = sell("s1", 100, participant_id=P1)
        s2 = sell("s2", 100, participant_id=P1)
        book.submit(s1, seq=1)
        book.submit(s2, seq=2)

        # when s1 is decreased to a total of 6, and then raised to 8
        decrease = book.amend("s1", price=100, quantity=6, seq=3)
        increase = book.amend("s1", price=100, quantity=8, seq=4)

        # then the decrease keeps priority, and 8 is an increase over 6: it loses
        # priority, even though 8 is below the original 10
        assert decrease == [Replaced("s1", seq=1, new_price=100, new_total_quantity=6)]
        assert increase == [Replaced("s1", seq=4, new_price=100, new_total_quantity=8)]
        assert book.orders_at(Side.SELL, 100) == ["s2", "s1"]
        assert_book_consistent(book)

        # when a market buy of 18 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=18, participant_id=P2)
        fills = book.submit(incoming, seq=5)

        # then s2 fills first, then s1 with 8
        assert fills == [
            Accepted(seq=5, order=incoming),
            make_trade(1, 100, 10, incoming, s2),
            make_trade(2, 100, 8, incoming, s1),
        ]
        assert_book_consistent(book)

    def test_decrease_after_losing_priority_keeps_the_new_place_and_seq(self) -> None:
        # given two resting sells of 10 at 100, s1 first
        book = OrderBook()
        s1 = sell("s1", 100, participant_id=P1)
        s2 = sell("s2", 100, participant_id=P1)
        book.submit(s1, seq=1)
        book.submit(s2, seq=2)

        # when s1 is increased to 15 (it goes to the back with seq 3), and then
        # decreased to 12
        increase = book.amend("s1", price=100, quantity=15, seq=3)
        decrease = book.amend("s1", price=100, quantity=12, seq=4)

        # then the decrease carries the seq 3 from the increase, not the original 1
        # and not its own 4, and s1 stays at the back
        assert increase == [Replaced("s1", seq=3, new_price=100, new_total_quantity=15)]
        assert decrease == [Replaced("s1", seq=3, new_price=100, new_total_quantity=12)]
        assert book.orders_at(Side.SELL, 100) == ["s2", "s1"]
        assert_book_consistent(book)

        # when a market buy of 22 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=22, participant_id=P2)
        fills = book.submit(incoming, seq=5)

        # then s2 fills first, then s1 with 12
        assert fills == [
            Accepted(seq=5, order=incoming),
            make_trade(1, 100, 10, incoming, s2),
            make_trade(2, 100, 12, incoming, s1),
        ]
        assert_book_consistent(book)

    def test_filled_quantity_is_measured_against_the_total_after_a_replace(
        self,
    ) -> None:
        # given a resting sell of 10 at 100 that is amended to a price of 101 and a
        # total of 20, which loses priority
        book = OrderBook()
        resting = sell("s1", 100, quantity=10, participant_id=P1)
        book.submit(resting, seq=1)
        book.amend("s1", price=101, quantity=20, seq=2)

        # and a buy of 8 at 101 fills 8 of it
        book.submit(buy("b1", 101, quantity=8, participant_id=P2), seq=3)

        # when it is amended to a total of 8, which equals what is filled
        at_filled = book.amend("s1", price=101, quantity=8, seq=4)

        # then it is rejected
        assert at_filled == [Rejected("s1", RejectReason.QUANTITY_BELOW_FILLED)]

        # when it is amended to a total of 9, a decrease from 20
        decrease = book.amend("s1", price=101, quantity=9, seq=5)

        # then it keeps priority and carries the seq 2 from the replace
        assert decrease == [Replaced("s1", seq=2, new_price=101, new_total_quantity=9)]
        assert_book_consistent(book)

        # when a market buy of 10 arrives
        incoming = create_market_order("m1", Side.BUY, quantity=10, participant_id=P2)
        fills = book.submit(incoming, seq=6)

        # then s1 has 1 left (9 total - 8 filled)
        assert fills == [
            Accepted(seq=6, order=incoming),
            make_trade(2, 101, 1, incoming, resting),
            Cancelled("m1", 9, CancelReason.IOC_REMAINDER),
        ]
        assert_book_consistent(book)


class TestAmendSelfTradePrevention:
    def test_amend_into_own_order_is_cancelled_in_full(self) -> None:
        # given a resting buy of 10 at 100 and a resting sell of 10 at 101, both from p1
        book = OrderBook()
        book.submit(buy("b1", 100, participant_id=P1), seq=1)
        own = sell("s1", 101, participant_id=P1)
        book.submit(own, seq=2)

        # when the buy is amended to a price of 101, where it would hit its own sell
        events = book.amend("b1", price=101, quantity=10, seq=3)

        # then Replaced is followed by a cancel, and the amended order is gone
        assert events == [
            Replaced("b1", seq=3, new_price=101, new_total_quantity=10),
            Cancelled("b1", 10, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert book.best_bid is None
        assert book.orders_at(Side.BUY, 100) == []
        assert book.orders_at(Side.BUY, 101) == []
        # and the resting sell is untouched
        assert book.orders_at(Side.SELL, 101) == ["s1"]
        assert book.cancel("b1") == [Rejected("b1", RejectReason.UNKNOWN_ORDER)]
        assert_book_consistent(book)

    def test_trades_before_the_self_trade_stand(self) -> None:
        # given resting sells of 4 from p2 and then 10 from p1, both at 101
        book = OrderBook()
        other = sell("s1", 101, quantity=4, participant_id=P2)
        book.submit(other, seq=1)
        book.submit(sell("s2", 101, quantity=10, participant_id=P1), seq=2)
        # and a resting buy of 10 at 100 from p1
        b1 = buy("b1", 100, participant_id=P1)
        book.submit(b1, seq=3)

        # when the buy is amended to a price of 101
        events = book.amend("b1", price=101, quantity=10, seq=4)

        # then it trades 4 against p2, and the other 6 are cancelled at the self-trade
        assert events == [
            Replaced("b1", seq=4, new_price=101, new_total_quantity=10),
            make_trade(1, 101, 4, b1, other),
            Cancelled("b1", 6, CancelReason.SELF_TRADE_PREVENTED),
        ]
        assert_no_self_trades(events)
        assert book.orders_at(Side.SELL, 101) == ["s2"]
        assert book.best_bid is None
        assert_no_trace(book, b1)
        assert_book_consistent(book)


class TestAmendIds:
    @pytest.mark.parametrize(
        ("price", "quantity"),
        [(100, 6), (100, 15), (98, 10)],
        ids=["keeps-priority", "loses-priority", "price-change"],
    )
    def test_amended_id_is_live_exactly_once(self, price: int, quantity: int) -> None:
        # given a resting buy of 10 at 100
        book = OrderBook()
        book.submit(buy("b1", 100, participant_id=P1), seq=1)

        # when it is amended
        book.amend("b1", price=price, quantity=quantity, seq=2)
        assert_book_consistent(book)

        # then it can be cancelled once, with its amended quantity
        assert book.cancel("b1") == [
            Cancelled("b1", quantity, CancelReason.USER_REQUESTED)
        ]
        # and a second cancel finds nothing
        assert book.cancel("b1") == [Rejected("b1", RejectReason.UNKNOWN_ORDER)]
        assert book.best_bid is None
        assert_book_consistent(book)

    def test_trade_ids_keep_counting_across_amend_and_submit(self) -> None:
        # given three resting sells of 5 at 100, 101 and 102
        book = OrderBook()
        for seq, price in enumerate([100, 101, 102], start=1):
            book.submit(sell(f"s{seq}", price, quantity=5, participant_id=P1), seq=seq)

        # when a buy trades by submit, then a resting buy trades by amend,
        # then another buy trades by submit
        first = book.submit(buy("b1", 100, quantity=5, participant_id=P2), seq=4)
        book.submit(buy("b2", 99, quantity=10, participant_id=P2), seq=5)
        amended = book.amend("b2", price=101, quantity=10, seq=6)
        last = book.submit(buy("b3", 102, quantity=5, participant_id=P2), seq=7)

        # then trade ids continue 1, 2, 3
        assert trade_ids(first) == [1]
        assert trade_ids(amended) == [2]
        assert trade_ids(last) == [3]
        assert_book_consistent(book)
