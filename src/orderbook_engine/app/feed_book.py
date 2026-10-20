"""The feed's own book: the log's events folded into totals per price.

For each log record the book works out which public messages follow from it
(see docs/FEED.md) and keeps its state in step. It holds the total quantity at
each price on each side, and a private index of the live orders, because a
cancel or a replace names an order but not its price. The index never leaves
this module. This module is pure: no I/O, no asyncio.

The fold has no matching logic and does not read time-in-force or order type.
An arriving order rests when it has quantity left after its trades and no
`Cancelled` event for it follows. A `Replaced` event whose seq equals the
order's own seq is an amend in place; any other `Replaced` takes the order off
its level and lets it arrive again.

After a `FeedError` the book is in an unknown state and must not be used.
"""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_engine.app.feed_messages import (
    FeedTrade,
    LevelUpdate,
    PriceLevel,
    Snapshot,
)
from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    Event,
    Rejected,
    Replaced,
)
from orderbook_engine.domain.models import Side, Trade


class FeedError(Exception):
    """A record the feed cannot apply: it disagrees with the feed's own state."""


@dataclass(slots=True)
class _Live:
    """An order resting on a level."""

    side: Side
    price: int
    total: int
    remaining: int
    seq: int


@dataclass(slots=True)
class _Arriving:
    """The order a record is placing: a submit, or an amend that lost priority."""

    order_id: str
    side: Side
    price: int | None  # None for a market order
    total: int
    seq: int
    unfilled: int
    cancelled: bool = False


type _Key = tuple[Side, int]


class FeedBook:
    """Totals per price on each side, and the feed's sequence number."""

    def __init__(self) -> None:
        self._levels: dict[Side, dict[int, int]] = {Side.BUY: {}, Side.SELL: {}}
        self._orders: dict[str, _Live] = {}
        self._seq = 0

    @property
    def seq(self) -> int:
        """The number of the last message published; 0 before the first."""
        return self._seq

    def snapshot(self) -> Snapshot:
        """Both sides as of `seq`, best price first."""
        bids = self._levels[Side.BUY]
        asks = self._levels[Side.SELL]
        return Snapshot(
            seq=self._seq,
            bids=tuple(PriceLevel(p, bids[p]) for p in sorted(bids, reverse=True)),
            asks=tuple(PriceLevel(p, asks[p]) for p in sorted(asks)),
        )

    def apply(self, record: LogRecord) -> tuple[LevelUpdate | FeedTrade, ...]:
        """Fold one record in and return the messages it publishes, numbered.

        The trades come first, in match order. Then come the levels whose total
        changed, once each with the total after the record: decreases first, then
        increases, each ordered by side (BUY first) and price. A record that
        changes nothing public returns an empty tuple and uses no number.
        """
        fold = _Fold(self._levels, self._orders, self._seq)
        for item in record.events:
            fold.event(item)
        messages = fold.finish()
        self._seq += len(messages)
        return messages


class _Fold:
    """The work of applying one record to the dictionaries of a `FeedBook`."""

    def __init__(
        self,
        levels: dict[Side, dict[int, int]],
        orders: dict[str, _Live],
        seq: int,
    ) -> None:
        self._levels = levels
        self._orders = orders
        self._seq = seq  # the number of the last message before this record
        self._before: dict[_Key, int] = {}  # level total before its first change
        self._trades: list[Trade] = []
        self._arriving: _Arriving | None = None

    def event(self, item: Event) -> None:
        match item:
            case Accepted(seq=seq, order=order):
                self._arriving = _Arriving(
                    order_id=order.order_id,
                    side=order.side,
                    price=order.price,
                    total=order.quantity,
                    seq=seq,
                    unfilled=order.quantity,
                )
            case Replaced():
                self._replaced(item)
            case Trade():
                self._trade(item)
            case Cancelled():
                self._cancelled(item)
            case Rejected():
                pass

    def _replaced(self, item: Replaced) -> None:
        live = self._live(item.order_id)
        if item.seq == live.seq:
            # Amend in place: same price, lower total, same queue position.
            lowered = live.total - item.new_total_quantity
            if item.new_price != live.price or lowered <= 0:
                raise FeedError(f"{item.order_id}: replaced in place, but not lower")
            self._add(live.side, live.price, -lowered)
            live.total = item.new_total_quantity
            live.remaining -= lowered
            if live.remaining <= 0:
                raise FeedError(f"{item.order_id}: nothing left after the amend")
            return
        # The order leaves its level and arrives again.
        filled = live.total - live.remaining
        self._add(live.side, live.price, -live.remaining)
        del self._orders[item.order_id]
        self._arriving = _Arriving(
            order_id=item.order_id,
            side=live.side,
            price=item.new_price,
            total=item.new_total_quantity,
            seq=item.seq,
            unfilled=item.new_total_quantity - filled,
        )

    def _trade(self, item: Trade) -> None:
        resting = self._orders.get(item.resting_order_id)
        if resting is None:
            raise FeedError(f"trade against unknown order {item.resting_order_id!r}")
        if (
            resting.side is item.aggressor_side
            or resting.price != item.price
            or resting.remaining < item.quantity
        ):
            raise FeedError(f"trade does not fit order {item.resting_order_id!r}")
        arriving = self._arriving
        if (
            arriving is None
            or arriving.order_id != item.aggressor_order_id
            or arriving.unfilled < item.quantity
        ):
            raise FeedError(f"trade by unknown aggressor {item.aggressor_order_id!r}")
        self._add(resting.side, resting.price, -item.quantity)
        resting.remaining -= item.quantity
        if resting.remaining == 0:
            del self._orders[item.resting_order_id]
        arriving.unfilled -= item.quantity
        self._trades.append(item)

    def _cancelled(self, item: Cancelled) -> None:
        arriving = self._arriving
        if arriving is not None and arriving.order_id == item.order_id:
            if arriving.unfilled != item.quantity:
                raise FeedError(f"{item.order_id}: cancelled quantity does not fit")
            arriving.cancelled = True
            return
        live = self._live(item.order_id)
        if live.remaining != item.quantity:
            raise FeedError(f"{item.order_id}: cancelled quantity does not fit")
        self._add(live.side, live.price, -live.remaining)
        del self._orders[item.order_id]

    def _live(self, order_id: str) -> _Live:
        live = self._orders.get(order_id)
        if live is None:
            raise FeedError(f"unknown order {order_id!r}")
        return live

    def _add(self, side: Side, price: int, delta: int) -> None:
        """Change a level's total, noting what it was before the record."""
        levels = self._levels[side]
        before = levels.get(price, 0)
        self._before.setdefault((side, price), before)
        total = before + delta
        if total < 0:
            raise FeedError(f"level {side} {price} would go below zero")
        if total == 0:
            levels.pop(price, None)
        else:
            levels[price] = total

    def finish(self) -> tuple[LevelUpdate | FeedTrade, ...]:
        """Rest the arriving order if it rests, then number the messages."""
        arriving = self._arriving
        if arriving is not None and not arriving.cancelled and arriving.unfilled > 0:
            if arriving.price is None:
                raise FeedError(f"{arriving.order_id}: a market order cannot rest")
            if arriving.order_id in self._orders:
                raise FeedError(f"{arriving.order_id}: id is already live")
            self._orders[arriving.order_id] = _Live(
                side=arriving.side,
                price=arriving.price,
                total=arriving.total,
                remaining=arriving.unfilled,
                seq=arriving.seq,
            )
            self._add(arriving.side, arriving.price, arriving.unfilled)

        after = {
            key: self._levels[key[0]].get(key[1], 0)
            for key in self._before
            if self._levels[key[0]].get(key[1], 0) != self._before[key]
        }
        decreases = [key for key in after if after[key] < self._before[key]]
        increases = [key for key in after if after[key] > self._before[key]]

        messages: list[LevelUpdate | FeedTrade] = []
        seq = self._seq
        for item in self._trades:
            seq += 1
            messages.append(
                FeedTrade(
                    seq=seq,
                    trade_id=item.trade_id,
                    price=item.price,
                    quantity=item.quantity,
                    aggressor_side=item.aggressor_side,
                )
            )
        for key in (*sorted(decreases, key=_order), *sorted(increases, key=_order)):
            seq += 1
            messages.append(
                LevelUpdate(seq=seq, side=key[0], price=key[1], quantity=after[key])
            )
        return tuple(messages)


def _order(key: _Key) -> tuple[bool, int]:
    """BUY before SELL, then ascending price."""
    return (key[0] is Side.SELL, key[1])
