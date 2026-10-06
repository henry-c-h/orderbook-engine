from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class _MatchResult:
    trades: list[Trade]
    remaining: int  # quantity of the incoming order left unfilled
    self_trade_prevented: bool  # matching stopped at an order of the same participant


class OrderBook:
    """Resting orders for one instrument. Synchronous and deterministic.

    Supports GTC limit orders (matching and resting), IOC limit orders, market
    orders and cancel, with cancel-newest self-trade prevention.
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
        if order.order_id in self._orders:
            return [Rejected(order.order_id, RejectReason.DUPLICATE_ORDER_ID)]

        # Build the event first: it validates seq before the book is touched.
        events: list[Event] = [Accepted(seq=seq, order=order)]
        result = self._match(order, order.price)
        events.extend(result.trades)
        remaining = result.remaining
        # A remainder after a self-trade is cancelled whatever the order type.
        # Otherwise a GTC remainder rests and an IOC or market remainder is cancelled.
        if remaining > 0:
            if result.self_trade_prevented:
                events.append(
                    Cancelled(
                        order.order_id, remaining, CancelReason.SELF_TRADE_PREVENTED
                    )
                )
            elif order.time_in_force is TimeInForce.GTC:
                self._rest(RestingOrder.from_order(order, seq, remaining))
            else:
                events.append(
                    Cancelled(order.order_id, remaining, CancelReason.IOC_REMAINDER)
                )
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

    def _match(self, incoming: Order, limit_price: int | None) -> _MatchResult:
        """Trade `incoming` against the opposite side while prices cross.

        `limit_price` is None for a market order, which crosses at any price.
        Reduces `remaining` on resting orders and removes filled orders and
        emptied levels. Matching stops at the first resting order from the same
        participant: nothing trades with it and it is left untouched.
        """
        side = incoming.side
        opposite_levels = self._levels(side.opposite)
        # Best opposite price: lowest ask for a buy, highest bid for a sell.
        best_index = 0 if side is Side.BUY else -1
        trades: list[Trade] = []
        remaining = incoming.quantity

        while remaining > 0 and opposite_levels:
            best_price, level = opposite_levels.peekitem(best_index)
            if not _crosses(side, limit_price, best_price):
                break

            resting = next(iter(level.values()))  # front of the level: oldest
            if resting.order.participant_id == incoming.participant_id:
                return _MatchResult(trades, remaining, self_trade_prevented=True)

            trade_quantity = min(remaining, resting.remaining)
            trades.append(
                Trade(
                    trade_id=self._next_trade_id,
                    price=best_price,  # the resting order's price
                    quantity=trade_quantity,
                    aggressor_order_id=incoming.order_id,
                    resting_order_id=resting.order.order_id,
                    aggressor_side=side,
                    aggressor_participant_id=incoming.participant_id,
                    resting_participant_id=resting.order.participant_id,
                )
            )
            self._next_trade_id += 1
            remaining -= trade_quantity
            resting.remaining -= trade_quantity

            if resting.remaining == 0:
                del level[resting.order.order_id]
                del self._orders[resting.order.order_id]
                if not level:
                    del opposite_levels[best_price]

        return _MatchResult(trades, remaining, self_trade_prevented=False)


def _crosses(side: Side, limit_price: int | None, resting_price: int) -> bool:
    """A buy crosses asks at or below its limit; a sell crosses bids at or above.

    No limit (a market order) crosses every price.
    """
    if limit_price is None:
        return True
    if side is Side.BUY:
        return resting_price <= limit_price
    return resting_price >= limit_price
