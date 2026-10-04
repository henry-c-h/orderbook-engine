import pytest

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Rejected,
    RejectReason,
)
from orderbook_engine.domain.models import Order, OrderType, Side, TimeInForce
from orderbook_engine.engine.order_book import OrderBook


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
    def test_crossing_buy_raises(self) -> None:
        book = OrderBook()
        book.submit(sell("s1", 100), seq=1)
        with pytest.raises(NotImplementedError):
            book.submit(buy("b1", 100), seq=2)

    def test_crossing_sell_raises(self) -> None:
        book = OrderBook()
        book.submit(buy("b1", 100), seq=1)
        with pytest.raises(NotImplementedError):
            book.submit(sell("s1", 99), seq=2)

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
