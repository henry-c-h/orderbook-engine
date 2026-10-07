"""Random command logs for the property tests, and the code that runs them.

The value ranges are small on purpose, so that random commands meet each
other: orders cross, self-trade, reuse ids and target live, finished and
unknown orders.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from helpers import BookState, book_state
from hypothesis import event
from hypothesis import strategies as st

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Rejected,
    Replaced,
)
from orderbook_engine.domain.models import (
    Order,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from orderbook_engine.engine.order_book import OrderBook


@dataclass(frozen=True, slots=True)
class Submit:
    order: Order


@dataclass(frozen=True, slots=True)
class Cancel:
    order_id: str


@dataclass(frozen=True, slots=True)
class Amend:
    order_id: str
    price: int
    quantity: int


type Command = Submit | Cancel | Amend

_PRICES = st.integers(min_value=95, max_value=105)
_QUANTITIES = st.integers(min_value=1, max_value=10)
_PARTICIPANT_IDS = ("p1", "p2", "p3")
_PARTICIPANTS = st.sampled_from(_PARTICIPANT_IDS)
_ORDER_IDS = st.sampled_from(tuple(f"o{n}" for n in range(1, 9)))
_SIDES = st.sampled_from(Side)


def _limit_orders(time_in_force: TimeInForce) -> st.SearchStrategy[Order]:
    return st.builds(
        Order,
        order_id=_ORDER_IDS,
        participant_id=_PARTICIPANTS,
        side=_SIDES,
        order_type=st.just(OrderType.LIMIT),
        quantity=_QUANTITIES,
        price=_PRICES,
        time_in_force=st.just(time_in_force),
    )


_market_orders = st.builds(
    Order,
    order_id=_ORDER_IDS,
    participant_id=_PARTICIPANTS,
    side=_SIDES,
    order_type=st.just(OrderType.MARKET),
    quantity=_QUANTITIES,
    price=st.none(),
    time_in_force=st.just(TimeInForce.IOC),
)

# GTC orders come up three times as often, because they build the book.
_orders = st.one_of(
    _limit_orders(TimeInForce.GTC),
    _limit_orders(TimeInForce.GTC),
    _limit_orders(TimeInForce.GTC),
    _limit_orders(TimeInForce.IOC),
    _market_orders,
)

_submits = st.builds(Submit, order=_orders)
_cancels = st.builds(Cancel, order_id=_ORDER_IDS)
_amends = st.builds(Amend, order_id=_ORDER_IDS, price=_PRICES, quantity=_QUANTITIES)

# Submits are most common, since the book needs orders for the rest to hit.
commands = st.one_of(_submits, _submits, _submits, _cancels, _amends, _amends)

# Hypothesis lists average about 5 elements, too few to build a book and then
# hit it. The long form supplies logs where the rare rules are reached. A
# failure still shrinks to a short log, because shrinking prefers the first form.
command_logs = st.one_of(
    st.lists(commands, max_size=60),
    st.lists(commands, min_size=20, max_size=60),
)


def apply(book: OrderBook, command: Command, seq: int) -> list[Event]:
    """Run one command on the book. `seq` is used by submits and amends."""
    match command:
        case Submit(order=order):
            return book.submit(order, seq)
        case Cancel(order_id=order_id):
            return book.cancel(order_id)
        case Amend(order_id=order_id, price=price, quantity=quantity):
            return book.amend(order_id, price, quantity, seq)


@st.composite
def logs_ending_in_an_amend_of_a_live_order(draw: st.DrawFn) -> list[Command]:
    """A random log whose last command amends an order that is live at that point.

    Independent draws rarely line up a live id, the order's own price and a
    lower quantity. Here the amend's price is the order's own price or a fresh
    one, so both the priority-keeping and the priority-losing amend are reached
    often. The log stays plain data: every command in it is concrete.
    """
    log = list(draw(command_logs))
    book = OrderBook()
    for seq, command in enumerate(log, start=1):
        apply(book, command, seq)

    live = book_state(book).orders
    if not live:
        # On an empty book a GTC limit order always rests.
        log.append(Submit(draw(_limit_orders(TimeInForce.GTC))))
        apply(book, log[-1], len(log))
        live = book_state(book).orders

    order_id = draw(st.sampled_from(sorted(live)))
    price = draw(st.one_of(st.just(live[order_id].price), _PRICES))
    quantity = draw(_QUANTITIES)
    return [*log, Amend(order_id, price, quantity)]


@st.composite
def logs_ending_in_a_sweep_of_one_price_level(draw: st.DrawFn) -> list[Command]:
    """A random log whose last command should fill several orders at one price.

    Independent draws almost never stack two orders at one price and then fill
    both with a single order. Here, after a random prefix, two or three resting
    orders from other participants are placed at one price, then an aggressor
    crosses that price with enough quantity to reach the second order. Time
    priority within a level is then exercised on most examples. The prefix can
    still match the stack first, so not every example ends in such a sweep.
    The log stays plain data: every command in it is concrete.
    """
    log = list(draw(command_logs))
    aggressor_side = draw(_SIDES)
    price = draw(_PRICES)
    aggressor_participant = draw(_PARTICIPANTS)
    other_participants = st.sampled_from(
        [p for p in _PARTICIPANT_IDS if p != aggressor_participant]
    )

    quantities = draw(st.lists(_QUANTITIES, min_size=2, max_size=3))
    for n, quantity in enumerate(quantities, start=1):
        stacked = Order(
            order_id=f"m{n}",
            participant_id=draw(other_participants),
            side=aggressor_side.opposite,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=price,
        )
        log.append(Submit(stacked))

    # The aggressor is willing to trade at the stack's price or better, and
    # wants more than the oldest stacked order holds.
    limit_price = draw(
        st.integers(min_value=price, max_value=105)
        if aggressor_side is Side.BUY
        else st.integers(min_value=95, max_value=price)
    )
    wanted = draw(st.integers(min_value=quantities[0] + 1, max_value=sum(quantities)))
    aggressor = Order(
        order_id="m_aggressor",
        participant_id=aggressor_participant,
        side=aggressor_side,
        order_type=OrderType.LIMIT,
        quantity=wanted,
        price=limit_price,
        time_in_force=draw(st.sampled_from(TimeInForce)),
    )
    return [*log, Submit(aggressor)]


@dataclass(frozen=True, slots=True)
class Step:
    """One command applied to the book, with the book before and after it."""

    seq: int
    command: Command
    before: BookState
    events: list[Event]
    after: BookState


def play(book: OrderBook, log: Sequence[Command]) -> Iterator[Step]:
    """Apply a log to `book`, numbering commands 1, 2, 3, ... as seq."""
    accepted_ids: set[str] = set()
    for seq, command in enumerate(log, start=1):
        before = book_state(book)
        events = apply(book, command, seq)
        step = Step(
            seq=seq,
            command=command,
            before=before,
            events=events,
            after=book_state(book),
        )
        _record_scenarios(step, accepted_ids)
        for item in events:
            if isinstance(item, Accepted):
                accepted_ids.add(item.order.order_id)
        yield step


def _record_scenarios(step: Step, accepted_ids: set[str]) -> None:
    """Tell Hypothesis which situations a step reached.

    `--hypothesis-show-statistics` then shows whether the strategies reach each
    rule, so that a property that never meets its situation cannot pass quietly.
    """
    trades = [item for item in step.events if isinstance(item, Trade)]
    cancels = [item for item in step.events if isinstance(item, Cancelled)]
    rejects = [item for item in step.events if isinstance(item, Rejected)]
    replaces = [item for item in step.events if isinstance(item, Replaced)]

    match step.command:
        case Submit(order=order):
            if rejects:
                event("submit rejected: duplicate id")
                return
            if order.order_id in accepted_ids:
                event("submit: id reused after the order finished")
            if order.order_type is OrderType.MARKET and not trades:
                event("submit: market order with no liquidity")
            if len({trade.price for trade in trades}) >= 2:
                event("submit: sweep across several price levels")
        case Cancel():
            event("cancel: unknown id" if rejects else "cancel: live order")
        case Amend():
            if rejects:
                event(f"amend rejected: {rejects[0].reason}")
                return
            if replaces[0].seq != step.seq:
                event("amend: kept its queue position")
            elif trades:
                event("amend: crossed the book")
            else:
                event("amend: lost its queue position without crossing")
    if len(trades) > len({trade.price for trade in trades}):
        event("several resting orders matched at one price")
    if any(item.reason is CancelReason.SELF_TRADE_PREVENTED for item in cancels):
        event("self-trade prevented")
