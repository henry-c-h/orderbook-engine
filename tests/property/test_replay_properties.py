"""Properties of replaying a random sequence of commands."""

from helpers import assert_book_consistent, book_state
from hypothesis import given
from strategies import command_logs, play

from orderbook_engine.domain.commands import Command
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.replay.replay import replay


class TestReplayProperties:
    @given(commands=command_logs)
    def test_replaying_the_same_commands_twice_gives_the_same_result(
        self, commands: list[Command]
    ) -> None:
        # given a random sequence of commands
        # when it is replayed twice
        first = replay(commands)
        second = replay(commands)

        # then the events and the final book states are equal
        assert first.events == second.events
        assert book_state(first.book) == book_state(second.book)

    @given(commands=command_logs)
    def test_replay_gives_the_same_events_as_play(
        self, commands: list[Command]
    ) -> None:
        # given a random sequence of commands and a book for `play`, the test
        # runner that applies commands one at a time with its own numbering
        book = OrderBook()

        # when the commands are run through `play` and through `replay`
        expected = [event for step in play(book, commands) for event in step.events]
        result = replay(commands)

        # then the events and the final book states are identical
        assert result.events == expected
        assert book_state(result.book) == book_state(book)

    @given(commands=command_logs)
    def test_the_replayed_book_is_consistent(self, commands: list[Command]) -> None:
        # given a random sequence of commands
        # when it is replayed
        result = replay(commands)

        # then the final book is internally consistent
        assert_book_consistent(result.book)
