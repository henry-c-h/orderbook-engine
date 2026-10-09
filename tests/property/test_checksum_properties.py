"""Properties of the state checksum over random sequences of commands."""

from hypothesis import given
from strategies import command_logs

from orderbook_engine.domain.commands import Command
from orderbook_engine.replay.checksum import encode_snapshot, state_checksum
from orderbook_engine.replay.replay import replay


class TestChecksumProperties:
    @given(commands=command_logs)
    def test_replaying_the_same_commands_twice_gives_the_same_checksum(
        self, commands: list[Command]
    ) -> None:
        # given a random sequence of commands
        # when it is replayed twice and each final book checksummed
        first = state_checksum(replay(commands).book.snapshot())
        second = state_checksum(replay(commands).book.snapshot())

        # then the checksums are equal
        assert first == second

    @given(first=command_logs, second=command_logs)
    def test_two_books_have_equal_encodings_and_checksums_only_if_they_are_equal(
        self, first: list[Command], second: list[Command]
    ) -> None:
        # given the final books of two random sequences of commands
        first_snapshot = replay(first).book.snapshot()
        second_snapshot = replay(second).book.snapshot()

        # when they are encoded and checksummed
        same_state = first_snapshot == second_snapshot
        same_encoding = encode_snapshot(first_snapshot) == encode_snapshot(
            second_snapshot
        )
        same_checksum = state_checksum(first_snapshot) == state_checksum(
            second_snapshot
        )

        # then the encodings and checksums are equal exactly when the states are:
        # nothing is left out of the encoding, and two states never encode alike
        assert same_encoding == same_state
        assert same_checksum == same_state
