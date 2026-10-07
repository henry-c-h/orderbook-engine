import dataclasses
from typing import Any

import pytest
from builders import valid_order, valid_trade

from orderbook_engine.domain.models import (
    OrderType,
    RestingOrder,
    Side,
    TimeInForce,
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
        order = valid_order(
            order_type=order_type, price=price, time_in_force=time_in_force
        )
        assert order.order_type is order_type
        assert order.price == price
        assert order.time_in_force is time_in_force

    def test_is_immutable(self) -> None:
        order = valid_order()
        with pytest.raises(dataclasses.FrozenInstanceError):
            order.quantity = 5  # type: ignore[reportAttributeAccessIssue]


class TestOrderIds:
    def test_rejects_empty_order_id(self) -> None:
        with pytest.raises(ValueError, match="order_id"):
            valid_order(order_id="")

    def test_rejects_empty_participant_id(self) -> None:
        with pytest.raises(ValueError, match="participant_id"):
            valid_order(participant_id="")


class TestOrderQuantity:
    @pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "10", None])
    def test_rejects_non_positive_or_non_int(self, quantity: Any) -> None:
        with pytest.raises(ValueError, match="quantity"):
            valid_order(quantity=quantity)


class TestLimitOrderPrice:
    @pytest.mark.parametrize("price", [None, 0, -5, True, 100.5, "100"])
    def test_rejects_missing_non_positive_or_non_int(self, price: Any) -> None:
        with pytest.raises(ValueError, match="price"):
            valid_order(price=price)


class TestMarketOrder:
    def test_rejects_price(self) -> None:
        with pytest.raises(ValueError, match="must not have a price"):
            valid_order(
                order_type=OrderType.MARKET,
                price=100,
                time_in_force=TimeInForce.IOC,
            )

    def test_rejects_gtc(self) -> None:
        with pytest.raises(ValueError, match="must be IOC"):
            valid_order(
                order_type=OrderType.MARKET,
                price=None,
                time_in_force=TimeInForce.GTC,
            )


class TestTrade:
    def test_accepts_positive_price_and_quantity(self) -> None:
        trade = valid_trade(price=100, quantity=10)
        assert trade.price == 100
        assert trade.quantity == 10

    @pytest.mark.parametrize("price", [0, -1, True, 100.5, "100", None])
    def test_rejects_invalid_price(self, price: Any) -> None:
        with pytest.raises(ValueError, match="price and quantity"):
            valid_trade(price=price)

    @pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "10", None])
    def test_rejects_invalid_quantity(self, quantity: Any) -> None:
        with pytest.raises(ValueError, match="price and quantity"):
            valid_trade(quantity=quantity)

    def test_is_immutable(self) -> None:
        trade = valid_trade()
        with pytest.raises(dataclasses.FrozenInstanceError):
            trade.price = 5  # type: ignore[reportAttributeAccessIssue]


class TestSide:
    def test_buy_opposite_is_sell(self) -> None:
        assert Side.BUY.opposite is Side.SELL

    def test_sell_opposite_is_buy(self) -> None:
        assert Side.SELL.opposite is Side.BUY


class TestRestingOrder:
    def test_from_order_starts_fully_unfilled(self) -> None:
        order = valid_order(quantity=10)
        resting = RestingOrder.from_order(order, seq=7)
        assert resting.order is order
        assert resting.seq == 7
        assert resting.remaining == 10

    def test_remaining_can_shrink_without_changing_the_order(self) -> None:
        order = valid_order(quantity=10)
        resting = RestingOrder.from_order(order, seq=1)
        resting.remaining -= 4
        assert resting.remaining == 6
        assert order.quantity == 10

    def test_from_order_takes_the_limit_price(self) -> None:
        # given a limit order at 100
        order = valid_order(price=100)

        # when it is placed on the book
        resting = RestingOrder.from_order(order, seq=1)

        # then the resting order carries that price
        assert resting.limit_price == 100

    def test_from_order_can_start_partly_filled(self) -> None:
        # given a limit order of 10
        order = valid_order(quantity=10)

        # when it is placed on the book with 6 remaining
        resting = RestingOrder.from_order(order, seq=1, remaining=6)

        # then it carries 6 remaining, and the order keeps its full quantity
        assert resting.remaining == 6
        assert order.quantity == 10

    @pytest.mark.parametrize("remaining", [0, -1, 11])
    def test_from_order_rejects_remaining_outside_the_order_quantity(
        self, remaining: int
    ) -> None:
        # given a limit order of 10
        order = valid_order(quantity=10)

        # when it is placed on the book with remaining of 0, negative or above 10,
        # then it is rejected
        with pytest.raises(ValueError, match="remaining"):
            RestingOrder.from_order(order, seq=1, remaining=remaining)

    def test_from_order_rejects_a_market_order(self) -> None:
        # given a market order, which has no price
        market = valid_order(
            order_type=OrderType.MARKET,
            price=None,
            time_in_force=TimeInForce.IOC,
        )

        # when it is placed on the book, then it is rejected
        with pytest.raises(ValueError, match="limit"):
            RestingOrder.from_order(market, seq=1)
