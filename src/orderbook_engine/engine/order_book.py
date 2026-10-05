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
from orderbook_engine.domain.models import (
    Order,
    RestingOrder,
    Side,
    TimeInForce,
    Trade,
)

# Orders at one price, in arrival order (= time priority).
type _Level = OrderedDict[str, RestingOrder]


class OrderBook:
    """Resting orders for one instrument. Synchronous and deterministic.

    Supports GTC limit orders (matching and resting) and cancel. Market and IOC
    orders and self-trade prevention are not supported yet.
    """

    def __init__(self) -> None:
        self._bids: SortedDict[int, _Level] = SortedDict()
        self._asks: SortedDict[int, _Level] = SortedDict()
        self._orders: dict[str, RestingOrder] = {}  # every live order, by id
        self._next_trade_id = 1

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
        if order.price is None or order.time_in_force is not TimeInForce.GTC:
            raise NotImplementedError("only GTC limit orders are supported so far")
        if order.order_id in self._orders:
            return [Rejected(order.order_id, RejectReason.DUPLICATE_ORDER_ID)]

        # Build the event first: it validates seq before the book is touched.
        events: list[Event] = [Accepted(seq=seq, order=order)]
        incoming = RestingOrder.from_order(order, seq)
        events.extend(self._match(incoming))
        if incoming.remaining > 0:
            self._rest(incoming)
        return events

    def cancel(self, order_id: str) -> list[Event]:
        resting = self._orders.pop(order_id, None)
        if resting is None:
            return [Rejected(order_id, RejectReason.UNKNOWN_ORDER)]

        levels = self._levels(resting.order.side)
        level = levels[resting.limit_price]
        del level[order_id]
        if not level:
            del levels[resting.limit_price]
        return [Cancelled(order_id, resting.remaining, CancelReason.USER_REQUESTED)]

    def _levels(self, side: Side) -> SortedDict[int, _Level]:
        return self._bids if side is Side.BUY else self._asks

    def _rest(self, resting: RestingOrder) -> None:
        levels = self._levels(resting.order.side)
        level = levels.setdefault(resting.limit_price, OrderedDict())
        level[resting.order.order_id] = resting
        self._orders[resting.order.order_id] = resting

    def _match(self, incoming: RestingOrder) -> list[Trade]:
        """Trade `incoming` against the opposite side while prices cross.

        Reduces `remaining` on both orders and removes filled orders and emptied
        levels. If the self-trade guard fires, the book may be partially mutated.
        """
        side = incoming.order.side
        opposite_levels = self._levels(side.opposite)
        # Best opposite price: lowest ask for a buy, highest bid for a sell.
        best_index = 0 if side is Side.BUY else -1
        trades: list[Trade] = []

        while incoming.remaining > 0 and opposite_levels:
            best_price, level = opposite_levels.peekitem(best_index)
            if not _crosses(side, incoming.limit_price, best_price):
                break

            resting = next(iter(level.values()))  # front of the level: oldest
            if resting.order.participant_id == incoming.order.participant_id:
                raise NotImplementedError("self-trade prevention is not supported yet")

            trade_quantity = min(incoming.remaining, resting.remaining)
            trades.append(
                Trade(
                    trade_id=self._next_trade_id,
                    price=best_price,  # the resting order's price
                    quantity=trade_quantity,
                    aggressor_order_id=incoming.order.order_id,
                    resting_order_id=resting.order.order_id,
                    aggressor_side=side,
                    aggressor_participant_id=incoming.order.participant_id,
                    resting_participant_id=resting.order.participant_id,
                )
            )
            self._next_trade_id += 1
            incoming.remaining -= trade_quantity
            resting.remaining -= trade_quantity

            if resting.remaining == 0:
                del level[resting.order.order_id]
                del self._orders[resting.order.order_id]
                if not level:
                    del opposite_levels[best_price]

        return trades


def _crosses(side: Side, limit_price: int, resting_price: int) -> bool:
    """A buy crosses asks at or below its limit; a sell crosses bids at or above."""
    if side is Side.BUY:
        return resting_price <= limit_price
    return resting_price >= limit_price
