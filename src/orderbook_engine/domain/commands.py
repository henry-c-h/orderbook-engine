from __future__ import annotations

from dataclasses import dataclass

from orderbook_engine.domain.models import Order, is_pos_int


@dataclass(frozen=True, slots=True)
class Submit:
    """Place a new order."""

    order: Order


@dataclass(frozen=True, slots=True)
class Cancel:
    """Cancel the live order with this id."""

    order_id: str

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must be non-empty")


@dataclass(frozen=True, slots=True)
class Amend:
    """Change the price and total quantity of the live order with this id.

    `quantity` is the new total, including anything already filled. Lowering
    it at the same price keeps the order's queue position; any other change
    is matched as a newly arriving order (see "Amend rules" in SPEC.md).
    """

    order_id: str
    price: int
    quantity: int

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must be non-empty")
        if not is_pos_int(self.price):
            raise ValueError(f"price must be a positive int, got {self.price!r}")
        if not is_pos_int(self.quantity):
            raise ValueError(f"quantity must be a positive int, got {self.quantity!r}")


type Command = Submit | Cancel | Amend
