from collections.abc import Iterable
from dataclasses import dataclass

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    Event,
    Rejected,
    Replaced,
)
from orderbook_engine.domain.models import Side, Trade


@dataclass(slots=True)
class OrderRecord:
    """What the event stream has said about one order so far."""

    order_id: str
    participant_id: str
    side: Side
    price: int | None  # None for a market order
    seq: int
    total: int
    filled: int = 0
    cancelled: int = 0


class OrderBlotter:
    """The running record of every order, built from the event stream alone.

    Tests compare it with the book. Only `Accepted` starts a record. `Trade`,
    `Cancelled` and `Replaced` update the record of the order they name.
    Order ids can be reused once an order is filled or cancelled, so a later
    `Accepted` with the same id starts a new record. `latest` is the record of
    the newest order with an id; `records` holds every record ever started,
    oldest first.
    """

    def __init__(self) -> None:
        self.latest: dict[str, OrderRecord] = {}
        self.records: list[OrderRecord] = []

    def apply(self, events: Iterable[Event]) -> None:
        for event in events:
            match event:
                case Accepted(seq=seq, order=order):
                    record = OrderRecord(
                        order_id=order.order_id,
                        participant_id=order.participant_id,
                        side=order.side,
                        price=order.price,
                        seq=seq,
                        total=order.quantity,
                    )
                    self.latest[order.order_id] = record
                    self.records.append(record)
                case Trade():
                    self.latest[event.aggressor_order_id].filled += event.quantity
                    self.latest[event.resting_order_id].filled += event.quantity
                case Cancelled():
                    self.latest[event.order_id].cancelled += event.quantity
                case Replaced():
                    record = self.latest[event.order_id]
                    record.price = event.new_price
                    record.seq = event.seq
                    record.total = event.new_total_quantity
                case Rejected():
                    pass
