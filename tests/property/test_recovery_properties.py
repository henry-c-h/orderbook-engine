"""Properties of rebuilding the book from a log over random commands."""

import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from strategies import command_logs, records_for

from orderbook_engine.adapters.file_log import FileLog
from orderbook_engine.app.recovery import recover_book
from orderbook_engine.domain.commands import Command
from orderbook_engine.replay.checksum import state_checksum
from orderbook_engine.replay.replay import replay

HEADER_SIZE = 8


class TestRecoveryProperties:
    @given(commands=command_logs)
    def test_the_recovered_book_has_the_checksum_of_the_replayed_book(
        self, commands: list[Command]
    ) -> None:
        # given the records of a random sequence of commands
        records = records_for(commands)

        # when the book is recovered from them, and the commands are replayed
        recovered = recover_book(records)
        replayed = replay(commands)

        # then the checksums of the two books are equal, and the next seq
        # follows the last command
        assert state_checksum(recovered.book.snapshot()) == state_checksum(
            replayed.book.snapshot()
        )
        assert recovered.next_seq == len(commands) + 1

    @given(commands=command_logs, data=st.data())
    def test_a_file_cut_anywhere_recovers_the_book_of_its_complete_prefix(
        self, commands: list[Command], data: st.DataObject
    ) -> None:
        # given a file log of a random sequence of commands, cut at a random
        # byte after the header
        records = records_for(commands)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.log"
            with FileLog(path, fsync=False) as log:
                for record in records:
                    log.append(record)
            content = path.read_bytes()
            cut = data.draw(st.integers(HEADER_SIZE, len(content)))
            path.write_bytes(content[:cut])

            # when the log is opened and the book recovered from what is left
            with FileLog(path, fsync=False) as reopened:
                kept = list(reopened.read_records())
                recovered = recover_book(kept)

                # then the records are a prefix of the commands', the file
                # holds the cut bytes less the dropped ones, and the book is
                # the one replay builds from that prefix
                assert kept == records[: len(kept)]
                assert path.stat().st_size + reopened.dropped_bytes == cut
                assert recovered.next_seq == reopened.next_seq
                assert state_checksum(recovered.book.snapshot()) == state_checksum(
                    replay(commands[: len(kept)]).book.snapshot()
                )
