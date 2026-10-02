from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from orderbook_engine.domain.models import Order, Trade, is_pos_int


class CancelReason(StrEnum):
    USER_REQUESTED = "USER_REQUESTED"
    IOC_REMAINDER = "IOC_REMAINDER"  # also: a market order that found no liquidity
    SELF_TRADE_PREVENTED = "SELF_TRADE_PREVENTED"


class RejectReason(StrEnum):
    UNKNOWN_ORDER = "UNKNOWN_ORDER"  # cancel/replace of an unknown or finished id


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
    """Cancel/replace: same order_id, new price and quantity, new `seq`."""

    order_id: str
    seq: int
    price: int
    quantity: int

    def __post_init__(self) -> None:
        if not is_pos_int(self.seq):
            raise ValueError(f"seq must be a positive int, got {self.seq!r}")
        if not is_pos_int(self.price) or not is_pos_int(self.quantity):
            raise ValueError("replaced price and quantity must be positive ints")


type Event = Accepted | Rejected | Trade | Cancelled | Replaced
