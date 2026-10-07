"""Properties of the order book under random command logs.

Each test applies a random log to a fresh book and checks one rule after every
command, so that a failure names the rule that broke. The shrunk log Hypothesis
reports is the shortest command sequence that breaks it.
"""

from itertools import takewhile

from helpers import (
    BookState,
    assert_index_matches_levels,
    assert_levels_well_formed,
    assert_not_crossed,
    assert_priority_order,
    book_state,
)
from hypothesis import given
from hypothesis import strategies as st
from order_blotter import OrderBlotter, OrderRecord
from strategies import (
    Amend,
    Cancel,
    Command,
    Step,
    Submit,
    command_logs,
    logs_ending_in_a_sweep_of_one_price_level,
    logs_ending_in_an_amend_of_a_live_order,
    play,
)

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Rejected,
    RejectReason,
    Replaced,
)
from orderbook_engine.domain.models import Side, TimeInForce, Trade
from orderbook_engine.engine.order_book import OrderBook


def _order_id(command: Command) -> str:
    match command:
        case Submit(order=order):
            return order.order_id
        case Cancel(order_id=order_id) | Amend(order_id=order_id):
            return order_id


def _allowed_rejections(command: Command) -> set[RejectReason]:
    match command:
        case Submit():
            return {RejectReason.DUPLICATE_ORDER_ID}
        case Cancel():
            return {RejectReason.UNKNOWN_ORDER}
        case Amend():
            return {RejectReason.UNKNOWN_ORDER, RejectReason.QUANTITY_BELOW_FILLED}


def _trades(events: list[Event]) -> list[Trade]:
    return [event for event in events if isinstance(event, Trade)]


def _resting_quantity(
    blotter: OrderBlotter, record: OrderRecord, state: BookState
) -> int:
    """How much of `record` the book still holds, 0 if none.

    Only the newest order with an id can rest: an older, finished order that
    shares its id must not be credited with the live order's quantity.
    """
    resting = state.orders.get(record.order_id)
    if resting is None or blotter.latest[record.order_id] is not record:
        return 0
    return resting.remaining


def _crosses(side: Side, limit_price: int | None, resting_price: int) -> bool:
    """A buy crosses asks at or below its limit, a sell the reverse."""
    if limit_price is None:
        return True
    if side is Side.BUY:
        return resting_price <= limit_price
    return resting_price >= limit_price


class TestBookStructure:
    @given(log=command_logs)
    def test_book_is_sound_and_never_crossed_after_every_command(
        self, log: list[Command]
    ) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when each command is applied
        for _ in play(book, log):
            # then the index matches the levels, the levels are well formed
            # and the book is not crossed
            assert_index_matches_levels(book)
            assert_levels_well_formed(book)
            assert_not_crossed(book)

    @given(log=command_logs)
    def test_orders_in_a_level_are_in_increasing_seq_order(
        self, log: list[Command]
    ) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when each command is applied
        for _ in play(book, log):
            # then within every level, seq strictly increases
            assert_priority_order(book)


class TestQuantityConservation:
    @given(log=command_logs)
    def test_total_is_filled_plus_cancelled_plus_resting_for_every_order(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter fed by its events, and a random log
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            blotter.apply(step.events)

            # then every order ever accepted balances against the book
            for record in blotter.records:
                resting = _resting_quantity(blotter, record, step.after)
                assert record.total == record.filled + record.cancelled + resting, (
                    f"{record} does not balance (resting {resting})"
                )

    @given(log=command_logs)
    def test_buyers_and_sellers_have_filled_the_same_quantity(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            blotter.apply(step.events)

            # then what buy orders have executed equals what sell orders have,
            # measured as total less cancelled less resting, not from the trades
            executed = {Side.BUY: 0, Side.SELL: 0}
            for record in blotter.records:
                resting = _resting_quantity(blotter, record, step.after)
                executed[record.side] += record.total - record.cancelled - resting
            assert executed[Side.BUY] == executed[Side.SELL]


class TestTrades:
    @given(log=command_logs)
    def test_trade_ids_run_from_one_without_gaps_or_repeats(
        self, log: list[Command]
    ) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when the whole log is applied
        trades = [trade for step in play(book, log) for trade in _trades(step.events)]

        # then the trade ids are exactly 1, 2, 3, ...
        assert [trade.trade_id for trade in trades] == list(range(1, len(trades) + 1))

    @given(log=command_logs)
    def test_trades_have_positive_price_and_quantity(self, log: list[Command]) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when the whole log is applied
        trades = [trade for step in play(book, log) for trade in _trades(step.events)]

        # then no trade has a non-positive price or quantity
        assert all(trade.price > 0 and trade.quantity > 0 for trade in trades)

    @given(log=command_logs)
    def test_trades_execute_at_the_resting_orders_price(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            blotter.apply(step.events)

            # then each trade is at its resting order's current limit price
            for trade in _trades(step.events):
                resting = blotter.latest[trade.resting_order_id]
                assert trade.price == resting.price, (
                    f"{trade} is not at the resting price of {resting}"
                )

    @given(log=command_logs)
    def test_no_trade_has_the_same_participant_on_both_sides(
        self, log: list[Command]
    ) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when the whole log is applied
        trades = [trade for step in play(book, log) for trade in _trades(step.events)]

        # then no trade is between a participant and itself
        assert all(
            trade.aggressor_participant_id != trade.resting_participant_id
            for trade in trades
        )

    @given(log=command_logs)
    def test_trades_stay_within_the_aggressors_limit_price(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            blotter.apply(step.events)

            # then a buy never pays above its limit and a sell never receives
            # less, and a market order (no limit) is unbounded
            for trade in _trades(step.events):
                aggressor = blotter.latest[trade.aggressor_order_id]
                assert trade.aggressor_side is aggressor.side
                if aggressor.price is not None:
                    assert _crosses(aggressor.side, aggressor.price, trade.price)

    @given(log=st.one_of(command_logs, logs_ending_in_a_sweep_of_one_price_level()))
    def test_trades_in_one_command_follow_price_then_time_priority(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log, half of which end in
        # an order that fills several orders stacked at one price
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            blotter.apply(step.events)

            # then the resting orders traded against are best price first,
            # oldest first within a price
            priorities = [
                (
                    trade.price if trade.aggressor_side is Side.BUY else -trade.price,
                    blotter.latest[trade.resting_order_id].seq,
                )
                for trade in _trades(step.events)
            ]
            assert priorities == sorted(priorities)


class TestEvents:
    @given(log=command_logs)
    def test_events_of_each_command_come_in_the_specified_order(
        self, log: list[Command]
    ) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when each command is applied
        for step in play(book, log):
            # then its events are in the order the spec gives for that command
            _assert_event_order(step)


def _assert_event_order(step: Step) -> None:
    events = step.events
    order_id = _order_id(step.command)
    first = events[0]

    if isinstance(first, Rejected):
        # a rejection stands alone and carries a reason valid for the command
        assert events == [first]
        assert first.order_id == order_id
        assert first.reason in _allowed_rejections(step.command)
        return

    if isinstance(step.command, Cancel):
        assert len(events) == 1
        assert isinstance(first, Cancelled)
        assert first.order_id == order_id
        assert first.reason is CancelReason.USER_REQUESTED
        return

    if isinstance(step.command, Submit):
        assert isinstance(first, Accepted)
        assert first.order.order_id == order_id
    else:
        assert isinstance(first, Replaced)
        assert first.order_id == order_id

    # then trades in match order, then at most one Cancelled, last
    after_first = events[1:]
    trades = list(takewhile(lambda event: isinstance(event, Trade), after_first))
    tail = after_first[len(trades) :]
    assert len(tail) <= 1
    for event in tail:
        assert isinstance(event, Cancelled)
        assert event.order_id == order_id
        assert event.reason in {
            CancelReason.IOC_REMAINDER,
            CancelReason.SELF_TRADE_PREVENTED,
        }


class TestRestingRules:
    @given(log=command_logs)
    def test_ioc_and_market_orders_never_rest(self, log: list[Command]) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when each command is applied
        for step in play(book, log):
            # then an accepted IOC or market order is not live afterwards
            command = step.command
            if (
                isinstance(command, Submit)
                and isinstance(step.events[0], Accepted)
                and command.order.time_in_force is TimeInForce.IOC
            ):
                assert command.order.order_id not in step.after.orders

    @given(log=command_logs)
    def test_ioc_remainder_is_cancelled_only_when_nothing_crossing_is_left(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            blotter.apply(step.events)

            for event in step.events:
                if isinstance(event, Cancelled) and (
                    event.reason is CancelReason.IOC_REMAINDER
                ):
                    # then the best order on the other side does not cross it
                    record = blotter.latest[event.order_id]
                    opposite = (
                        step.after.asks if record.side is Side.BUY else step.after.bids
                    )
                    assert not opposite or not _crosses(
                        record.side, record.price, opposite[0].price
                    )

    @given(log=command_logs)
    def test_self_trade_prevention_cancels_only_in_front_of_the_same_participant(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            blotter.apply(step.events)

            for event in step.events:
                if isinstance(event, Cancelled) and (
                    event.reason is CancelReason.SELF_TRADE_PREVENTED
                ):
                    # then the next order in line crosses and is the same
                    # participant's
                    record = blotter.latest[event.order_id]
                    opposite = (
                        step.after.asks if record.side is Side.BUY else step.after.bids
                    )
                    assert opposite
                    assert _crosses(record.side, record.price, opposite[0].price)
                    assert opposite[0].participant_id == record.participant_id


class TestRejectionsAndAmends:
    @given(log=command_logs)
    def test_commands_are_rejected_exactly_when_the_spec_says(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            live = step.before.orders
            order_id = _order_id(step.command)
            rejection = step.events[0] if isinstance(step.events[0], Rejected) else None

            # then a submit is rejected if its id is live, a cancel if its id
            # is not live, and an amend if its id is not live or its new total
            # does not exceed what is filled
            match step.command:
                case Submit():
                    expected = (
                        RejectReason.DUPLICATE_ORDER_ID if order_id in live else None
                    )
                case Cancel():
                    expected = None if order_id in live else RejectReason.UNKNOWN_ORDER
                case Amend(quantity=quantity):
                    if order_id not in live:
                        expected = RejectReason.UNKNOWN_ORDER
                    elif quantity <= blotter.latest[order_id].filled:
                        expected = RejectReason.QUANTITY_BELOW_FILLED
                    else:
                        expected = None
            assert (rejection.reason if rejection else None) == expected

            blotter.apply(step.events)

    @given(log=command_logs)
    def test_a_rejected_command_leaves_the_book_unchanged(
        self, log: list[Command]
    ) -> None:
        # given a fresh book and a random command log
        book = OrderBook()

        # when each command is applied
        for step in play(book, log):
            # then a rejected command changed nothing
            if isinstance(step.events[0], Rejected):
                assert step.after == step.before

    @given(log=logs_ending_in_an_amend_of_a_live_order())
    def test_amend_keeps_priority_only_when_lowering_quantity_at_the_same_price(
        self, log: list[Command]
    ) -> None:
        # given a fresh book, a blotter and a random log that ends by amending
        # a live order, so that this rule is exercised on most examples
        book = OrderBook()
        blotter = OrderBlotter()

        # when each command is applied
        for step in play(book, log):
            command = step.command
            if isinstance(command, Amend) and not isinstance(step.events[0], Rejected):
                record = blotter.latest[command.order_id]
                keeps_priority = (
                    command.price == record.price and command.quantity < record.total
                )
                if keeps_priority:
                    # then only Replaced is emitted, with the original seq, and
                    # the order stays where it was in its level
                    assert step.events == [
                        Replaced(
                            command.order_id,
                            record.seq,
                            command.price,
                            command.quantity,
                        )
                    ]
                    after = step.after.orders[command.order_id]
                    assert after.seq == record.seq
                    assert after.remaining == command.quantity - record.filled
                    assert _queue_at(step.before, record.side, command.price) == (
                        _queue_at(step.after, record.side, command.price)
                    )
                else:
                    # then it takes the command's seq, and if it rests it does so
                    # at the new price with that seq
                    replaced = step.events[0]
                    assert isinstance(replaced, Replaced)
                    assert replaced.seq == step.seq
                    resting = step.after.orders.get(command.order_id)
                    if resting is not None:
                        assert resting.seq == step.seq
                        assert resting.price == command.price

            blotter.apply(step.events)


def _queue_at(state: BookState, side: Side, price: int) -> list[str]:
    orders = state.bids if side is Side.BUY else state.asks
    return [order.order_id for order in orders if order.price == price]


class TestDeterminism:
    @given(log=command_logs)
    def test_same_log_on_two_fresh_books_gives_identical_events_and_state(
        self, log: list[Command]
    ) -> None:
        # given two fresh books and a random command log
        first_book = OrderBook()
        second_book = OrderBook()

        # when the same log is applied to each
        first_events = [step.events for step in play(first_book, log)]
        second_events = [step.events for step in play(second_book, log)]

        # then the events and the final book states are identical
        assert first_events == second_events
        assert book_state(first_book) == book_state(second_book)
