from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from orderbook_engine.domain.models import Order, Trade, is_pos_int


class CancelReason(StrEnum):
    USER_REQUESTED = "USER_REQUESTED"
    IOC_REMAINDER = "IOC_REMAINDER"  # also: a market order that found no liquidity
    SELF_TRADE_PREVENTED = "SELF_TRADE_PREVENTED"


class RejectReason(StrEnum):
    UNKNOWN_ORDER = "UNKNOWN_ORDER"  # cancel/amend of an unknown or finished id
    QUANTITY_BELOW_FILLED = "QUANTITY_BELOW_FILLED"  # amend total <= already filled
    DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID"  # submit with an id that is still live


@dataclass(frozen=True, slots=True)
class Accepted:
    """An order was accepted. Carries the full order so a replica can rebuild it."""

    seq: int  # seq represents the time-priority position
    order: Order

    def __post_init__(self) -> None:
        if not is_pos_int(self.seq):
            raise ValueError(f"seq must be a positive int, got {self.seq!r}")


@dataclass(frozen=True, slots=True)
class Rejected:
    order_id: str
    reason: RejectReason


@dataclass(frozen=True, slots=True)
class Cancelled:
    """`quantity` is the unfilled amount that was cancelled."""

    order_id: str
    quantity: int
    reason: CancelReason

    def __post_init__(self) -> None:
        if not is_pos_int(self.quantity):
            raise ValueError(
                f"cancelled quantity must be a positive int, got {self.quantity!r}"
            )


@dataclass(frozen=True, slots=True)
class Replaced:
    """Cancel/replace: same order_id, new price and total quantity, new `seq`.

    `new_total_quantity` includes anything already filled. A replica derives
    the remaining quantity as total minus the filled quantity from the trades
    it has already applied.
    """

    order_id: str
    seq: int
    new_price: int
    new_total_quantity: int

    def __post_init__(self) -> None:
        if not is_pos_int(self.seq):
            raise ValueError(f"seq must be a positive int, got {self.seq!r}")
        if not is_pos_int(self.new_price):
            raise ValueError(
                f"new_price must be a positive int, got {self.new_price!r}"
            )
        if not is_pos_int(self.new_total_quantity):
            raise ValueError(
                "new_total_quantity must be a positive int, "
                f"got {self.new_total_quantity!r}"
            )


type Event = Accepted | Rejected | Trade | Cancelled | Replaced
