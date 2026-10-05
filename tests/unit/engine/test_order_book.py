import pytest

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Rejected,
    RejectReason,
)
from orderbook_engine.domain.models import (
    Order,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from orderbook_engine.engine.order_book import OrderBook

# Matching tests need two different participants (same-participant matches are
# self-trades, which are not supported yet).
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


class TestSubmitNotYetSupported:
    def test_ioc_limit_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            OrderBook().submit(buy("b1", 100, time_in_force=TimeInForce.IOC), seq=1)

    def test_market_order_raises(self) -> None:
        market = Order(
            order_id="m1",
            participant_id="p1",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=10,
            time_in_force=TimeInForce.IOC,
        )
        with pytest.raises(NotImplementedError):
            OrderBook().submit(market, seq=1)


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


class TestSelfTradeGuard:
    """Temporary guard: self-trade prevention is not built yet."""

    def test_crossing_same_participant_raises(self) -> None:
        # given a resting sell from p1
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)

        # when a crossing buy from p1 arrives, then the guard raises
        with pytest.raises(NotImplementedError, match="self-trade"):
            book.submit(buy("b1", 100, participant_id=P1), seq=2)

    def test_guard_checks_every_resting_order_it_would_match(self) -> None:
        # given a resting sell from p1 at 100, then a resting sell from p2 at 101
        book = OrderBook()
        book.submit(sell("s1", 100, participant_id=P1), seq=1)
        book.submit(sell("s2", 101, participant_id=P2), seq=2)

        # when a buy of 20 from p2 sweeps up to 101, then the guard raises:
        # s1 is fine, but the sweep then reaches s2, the same participant
        with pytest.raises(NotImplementedError, match="self-trade"):
            book.submit(buy("b1", 101, quantity=20, participant_id=P2), seq=3)
