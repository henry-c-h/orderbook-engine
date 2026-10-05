from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    GTC = "GTC"  # good till cancelled: remainder rests on the book
    IOC = "IOC"  # immediate or cancel: remainder is cancelled


def is_pos_int(value: object) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return type(value) is int and value > 0


@dataclass(frozen=True, slots=True)
class Order:
    """An immutable instruction submitted to the engine."""

    order_id: str
    participant_id: str
    side: Side
    order_type: OrderType
    quantity: int
    price: int | None = None
    time_in_force: TimeInForce = TimeInForce.GTC

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must be non-empty")
        if not self.participant_id:
            raise ValueError("participant_id must be non-empty")
        if not is_pos_int(self.quantity):
            raise ValueError(f"quantity must be a positive int, got {self.quantity!r}")
        if self.order_type is OrderType.LIMIT:
            if not is_pos_int(self.price):
                raise ValueError(
                    f"limit order needs a positive int price, got {self.price!r}"
                )
        else:
            if self.price is not None:
                raise ValueError("market order must not have a price")
            if self.time_in_force is not TimeInForce.IOC:
                raise ValueError("market order must be IOC")


@dataclass(frozen=True, slots=True)
class Trade:
    """An execution. Price is always the resting (maker) order's price."""

    trade_id: int
    price: int
    quantity: int
    aggressor_order_id: str
    resting_order_id: str
    aggressor_side: Side
    aggressor_participant_id: str
    resting_participant_id: str

    def __post_init__(self) -> None:
        if not is_pos_int(self.price) or not is_pos_int(self.quantity):
            raise ValueError("trade price and quantity must be positive ints")


@dataclass(slots=True)
class RestingOrder:
    """An order sitting on the book, waiting to be matched. Mutable: remaining shrinks as it fills."""

    order: Order
    seq: int
    remaining: int
    limit_price: int

    @classmethod
    def from_order(cls, order: Order, seq: int) -> RestingOrder:
        if order.price is None:
            raise ValueError("only a limit order can rest on the book")
        return cls(
            order=order,
            seq=seq,
            remaining=order.quantity,
            limit_price=order.price,
        )
