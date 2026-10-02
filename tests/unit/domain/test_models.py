import dataclasses
from typing import Any

import pytest

from orderbook_engine.domain.models import (
    Order,
    OrderType,
    RestingOrder,
    Side,
    TimeInForce,
    Trade,
)


def make_order(
    *,
    order_id: Any = "o1",
    participant_id: Any = "p1",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.LIMIT,
    quantity: Any = 10,
    price: Any = 100,
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> Order:
    """Build a valid limit GTC order; override one field per test.

    Fields are typed Any so tests can pass deliberately invalid values.
    """
    return Order(
        order_id=order_id,
        participant_id=participant_id,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        time_in_force=time_in_force,
    )


class TestOrderValid:
    @pytest.mark.parametrize(
        ("order_type", "price", "time_in_force"),
        [
            (OrderType.LIMIT, 100, TimeInForce.GTC),
            (OrderType.LIMIT, 100, TimeInForce.IOC),
            (OrderType.MARKET, None, TimeInForce.IOC),
        ],
    )
    def test_accepts_valid_combinations(
        self, order_type: OrderType, price: int | None, time_in_force: TimeInForce
    ) -> None:
        order = make_order(
            order_type=order_type, price=price, time_in_force=time_in_force
        )
        assert order.order_type is order_type
        assert order.price == price
        assert order.time_in_force is time_in_force

    def test_is_immutable(self) -> None:
        order = make_order()
        with pytest.raises(dataclasses.FrozenInstanceError):
            order.quantity = 5  # type: ignore[reportAttributeAccessIssue]


class TestOrderIds:
    def test_rejects_empty_order_id(self) -> None:
        with pytest.raises(ValueError, match="order_id"):
            make_order(order_id="")

    def test_rejects_empty_participant_id(self) -> None:
        with pytest.raises(ValueError, match="participant_id"):
            make_order(participant_id="")


class TestOrderQuantity:
    @pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "10", None])
    def test_rejects_non_positive_or_non_int(self, quantity: Any) -> None:
        with pytest.raises(ValueError, match="quantity"):
            make_order(quantity=quantity)


class TestLimitOrderPrice:
    @pytest.mark.parametrize("price", [None, 0, -5, True, 100.5, "100"])
    def test_rejects_missing_non_positive_or_non_int(self, price: Any) -> None:
        with pytest.raises(ValueError, match="price"):
            make_order(price=price)


class TestMarketOrder:
    def test_rejects_price(self) -> None:
        with pytest.raises(ValueError, match="must not have a price"):
            make_order(
                order_type=OrderType.MARKET,
                price=100,
                time_in_force=TimeInForce.IOC,
            )

    def test_rejects_gtc(self) -> None:
        with pytest.raises(ValueError, match="must be IOC"):
            make_order(
                order_type=OrderType.MARKET,
                price=None,
                time_in_force=TimeInForce.GTC,
            )


def make_trade(*, price: Any = 100, quantity: Any = 10) -> Trade:
    """Build a valid trade; price and quantity are Any to allow invalid values."""
    return Trade(
        trade_id=1,
        price=price,
        quantity=quantity,
        aggressor_order_id="o2",
        resting_order_id="o1",
        aggressor_side=Side.BUY,
        aggressor_participant_id="p2",
        resting_participant_id="p1",
    )


class TestTrade:
    def test_accepts_positive_price_and_quantity(self) -> None:
        trade = make_trade(price=100, quantity=10)
        assert trade.price == 100
        assert trade.quantity == 10

    @pytest.mark.parametrize("price", [0, -1, True, 100.5, "100", None])
    def test_rejects_invalid_price(self, price: Any) -> None:
        with pytest.raises(ValueError, match="price and quantity"):
            make_trade(price=price)

    @pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "10", None])
    def test_rejects_invalid_quantity(self, quantity: Any) -> None:
        with pytest.raises(ValueError, match="price and quantity"):
            make_trade(quantity=quantity)

    def test_is_immutable(self) -> None:
        trade = make_trade()
        with pytest.raises(dataclasses.FrozenInstanceError):
            trade.price = 5  # type: ignore[reportAttributeAccessIssue]


class TestSide:
    def test_buy_opposite_is_sell(self) -> None:
        assert Side.BUY.opposite is Side.SELL

    def test_sell_opposite_is_buy(self) -> None:
        assert Side.SELL.opposite is Side.BUY


class TestRestingOrder:
    def test_from_order_starts_fully_unfilled(self) -> None:
        order = make_order(quantity=10)
        resting = RestingOrder.from_order(order, seq=7)
        assert resting.order is order
        assert resting.seq == 7
        assert resting.remaining == 10

    def test_remaining_can_shrink_without_changing_the_order(self) -> None:
        order = make_order(quantity=10)
        resting = RestingOrder.from_order(order, seq=1)
        resting.remaining -= 4
        assert resting.remaining == 6
        assert order.quantity == 10
