from __future__ import annotations

from collections import OrderedDict

from sortedcontainers import SortedDict

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Rejected,
    RejectReason,
)
from orderbook_engine.domain.models import Order, RestingOrder, Side, TimeInForce

# Orders at one price, in arrival order (= time priority).
type _Level = OrderedDict[str, RestingOrder]


class OrderBook:
    """Resting orders for one instrument. Synchronous and deterministic.

    This slice only rests non-crossing GTC limit orders and cancels them.
    """

    def __init__(self) -> None:
        self._bids: SortedDict[int, _Level] = SortedDict()
        self._asks: SortedDict[int, _Level] = SortedDict()
        self._orders: dict[str, RestingOrder] = {}  # every live order, by id

    @property
    def best_bid(self) -> int | None:
        return self._bids.peekitem(-1)[0] if self._bids else None

    @property
    def best_ask(self) -> int | None:
        return self._asks.peekitem(0)[0] if self._asks else None

    def orders_at(self, side: Side, price: int) -> list[str]:
        """Order ids at one price level, in priority order."""
        level = self._levels(side).get(price)
        return list(level) if level else []

    def submit(self, order: Order, seq: int) -> list[Event]:
        price = order.price
        if price is None or order.time_in_force is not TimeInForce.GTC:
            raise NotImplementedError("only GTC limit orders are supported so far")
        if self._crosses(order.side, price):
            raise NotImplementedError("crossing orders are not supported yet")
        if order.order_id in self._orders:
            return [Rejected(order.order_id, RejectReason.DUPLICATE_ORDER_ID)]

        # Build the event first: it validates seq before the book is touched.
        accepted = Accepted(seq=seq, order=order)
        resting = RestingOrder.from_order(order, seq)
        level = self._levels(order.side).setdefault(price, OrderedDict())
        level[order.order_id] = resting
        self._orders[order.order_id] = resting
        return [accepted]

    def cancel(self, order_id: str) -> list[Event]:
        resting = self._orders.pop(order_id, None)
        if resting is None:
            return [Rejected(order_id, RejectReason.UNKNOWN_ORDER)]

        side = resting.order.side
        price = resting.order.price
        assert price is not None  # only limit orders rest
        levels = self._levels(side)
        level = levels[price]
        del level[order_id]
        if not level:
            del levels[price]
        return [Cancelled(order_id, resting.remaining, CancelReason.USER_REQUESTED)]

    def _levels(self, side: Side) -> SortedDict[int, _Level]:
        return self._bids if side is Side.BUY else self._asks

    def _crosses(self, side: Side, price: int) -> bool:
        if side is Side.BUY:
            best_ask = self.best_ask
            return best_ask is not None and price >= best_ask
        best_bid = self.best_bid
        return best_bid is not None and price <= best_bid
