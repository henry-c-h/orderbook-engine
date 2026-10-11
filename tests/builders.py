"""Builders for the orders, trades and log records that tests need.

Each builder gives a valid object with sensible defaults, so that a test names
only the fields it cares about.
"""

from typing import Any

from strategies import records_for

from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.domain.commands import Cancel, Submit
from orderbook_engine.domain.models import (
    Order,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)


def limit_order(
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
    return limit_order(
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
    return limit_order(
        order_id,
        Side.SELL,
        price,
        quantity=quantity,
        participant_id=participant_id,
        time_in_force=time_in_force,
    )


def market_order(
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


def valid_order(
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


def valid_trade(*, price: Any = 100, quantity: Any = 10) -> Trade:
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


def expected_trade(
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


def three_records() -> list[LogRecord]:
    """A rest, a trade and a rejected cancel, as records with seq 1 to 3."""
    return records_for(
        [
            Submit(buy("b1", 100, participant_id="p1")),
            Submit(sell("s1", 100, quantity=4, participant_id="p2")),
            Cancel("nope"),
        ]
    )
