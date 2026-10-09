"""The book snapshot agrees with the test helper that reads the book directly."""

from itertools import groupby

from helpers import RestingState
from hypothesis import given
from strategies import command_logs, play

from orderbook_engine.domain.commands import Command
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.engine.snapshot import LevelSnapshot


def _flatten(
    levels: tuple[LevelSnapshot, ...],
) -> list[tuple[str, str, int, int, int, int]]:
    return [
        (
            order.order_id,
            order.participant_id,
            level.price,
            order.seq,
            order.total_quantity,
            order.remaining,
        )
        for level in levels
        for order in level.orders
    ]


def _expected(
    states: tuple[RestingState, ...],
) -> list[tuple[str, str, int, int, int, int]]:
    return [
        (
            state.order_id,
            state.participant_id,
            state.price,
            state.seq,
            state.total_quantity,
            state.remaining,
        )
        for state in states
    ]


def _levels(levels: tuple[LevelSnapshot, ...]) -> list[tuple[int, list[str]]]:
    return [
        (level.price, [order.order_id for order in level.orders]) for level in levels
    ]


def _grouped(states: tuple[RestingState, ...]) -> list[tuple[int, list[str]]]:
    """The helper's orders, which come best first, grouped by price."""
    return [
        (price, [state.order_id for state in group])
        for price, group in groupby(states, key=lambda state: state.price)
    ]


class TestSnapshotMatchesBookState:
    @given(commands=command_logs)
    def test_snapshot_agrees_with_book_state_after_every_command(
        self, commands: list[Command]
    ) -> None:
        # given a fresh book and a random sequence of commands
        book = OrderBook()

        # when each command is applied
        for step in play(book, commands):
            snapshot = book.snapshot()

            # then the snapshot lists the same orders, in the same order, with
            # the same fields as the helper that reads the book directly
            assert _flatten(snapshot.bids) == _expected(step.after.bids)
            assert _flatten(snapshot.asks) == _expected(step.after.asks)
            assert snapshot.next_trade_id == step.after.next_trade_id

    @given(commands=command_logs)
    def test_snapshot_groups_orders_into_the_same_levels_as_book_state(
        self, commands: list[Command]
    ) -> None:
        # given a fresh book and a random sequence of commands
        book = OrderBook()

        # when each command is applied
        for step in play(book, commands):
            snapshot = book.snapshot()

            # then the levels, with their order ids, are exactly the helper's
            # orders grouped by price: one level per price, none empty
            assert _levels(snapshot.bids) == _grouped(step.after.bids)
            assert _levels(snapshot.asks) == _grouped(step.after.asks)
