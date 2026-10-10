"""Properties of the log record codec and the file log over random commands."""

import tempfile
from pathlib import Path

from hypothesis import given
from strategies import command_logs, records_for

from orderbook_engine.adapters.file_log import FileLog
from orderbook_engine.app.log_record import decode_record, encode_record
from orderbook_engine.domain.commands import Command


class TestCodecProperties:
    @given(commands=command_logs)
    def test_every_record_survives_encode_and_decode(
        self, commands: list[Command]
    ) -> None:
        # given the records of a random sequence of commands
        records = records_for(commands)

        # when each is encoded and decoded
        decoded = [decode_record(encode_record(record)) for record in records]

        # then the results equal the originals
        assert decoded == records


class TestFileLogProperties:
    @given(commands=command_logs)
    def test_a_log_reads_back_the_records_appended_to_it(
        self, commands: list[Command]
    ) -> None:
        # given the records of a random sequence of commands, and a directory
        # that only this example uses
        records = records_for(commands)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.log"

            # when they are appended, and the file is closed and opened again
            with FileLog(path, fsync=False) as log:
                for record in records:
                    log.append(record)
            with FileLog(path, fsync=False) as reopened:
                # then they read back equal, and numbering continues after them
                assert list(reopened.read_records()) == records
                assert reopened.next_seq == len(records) + 1
