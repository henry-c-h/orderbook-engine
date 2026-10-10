import os
import struct
import zlib
from pathlib import Path

import pytest
from builders import buy, sell

from orderbook_engine.adapters.file_log import FileLog
from orderbook_engine.app.log_record import (
    LogFormatError,
    LogRecord,
    decode_record,
    encode_record,
)
from orderbook_engine.domain.commands import Cancel, Command, Submit
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.replay.replay import apply_command

HEADER = b"OBEVLOG\x01"


def records_for(commands: list[Command]) -> list[LogRecord]:
    """The records the sequencer would write for `commands`, seq 1, 2, 3, ..."""
    book = OrderBook()
    return [
        LogRecord(seq, command, tuple(apply_command(book, command, seq)))
        for seq, command in enumerate(commands, start=1)
    ]


def three_records() -> list[LogRecord]:
    """A rest, a trade and a rejected cancel."""
    return records_for(
        [
            Submit(buy("b1", 100, participant_id="p1")),
            Submit(sell("s1", 100, quantity=4, participant_id="p2")),
            Cancel("nope"),
        ]
    )


def frame(payload: bytes) -> bytes:
    """A record frame, built here independently of the code under test."""
    length = struct.pack(">I", len(payload))
    return length + struct.pack(">I", zlib.crc32(length + payload)) + payload


class TestNewFile:
    def test_starts_with_the_header_and_no_records(self, tmp_path: Path) -> None:
        # given a path where no file exists
        path = tmp_path / "events.log"

        # when a log is opened there
        with FileLog(path) as log:
            # then the file holds only the header, the log is empty and the
            # first record must have seq 1
            assert path.read_bytes() == HEADER
            assert list(log.read_records()) == []
            assert log.next_seq == 1

    def test_an_empty_existing_file_is_started_as_a_new_log(
        self, tmp_path: Path
    ) -> None:
        # given an existing file with no bytes in it
        path = tmp_path / "events.log"
        path.touch()

        # when a log is opened there
        with FileLog(path):
            # then it gets the header
            assert path.read_bytes() == HEADER


class TestAppendAndRead:
    def test_records_read_back_equal_to_the_records_appended(
        self, tmp_path: Path
    ) -> None:
        # given an open log and three records
        records = three_records()
        with FileLog(tmp_path / "events.log") as log:
            # when they are appended
            for record in records:
                log.append(record)

            # then they read back in order, equal to what was written
            assert list(log.read_records()) == records
            assert log.next_seq == 4

    def test_each_record_is_framed_with_length_checksum_and_payload(
        self, tmp_path: Path
    ) -> None:
        # given a log holding one record
        path = tmp_path / "events.log"
        record = three_records()[0]
        with FileLog(path) as log:
            log.append(record)

        # when the file's bytes are read after the header
        data = path.read_bytes()[len(HEADER) :]
        length, crc = struct.unpack(">II", data[:8])
        payload = data[8:]

        # then the length counts the payload, the checksum covers the length
        # bytes and the payload, and the payload is the encoded record
        assert length == len(payload)
        assert crc == zlib.crc32(data[:4] + payload)
        assert decode_record(payload) == record

    def test_a_record_with_the_wrong_seq_is_refused(self, tmp_path: Path) -> None:
        # given a log holding one record, and records with seq 1 again and seq 3
        first, second, third = three_records()
        with FileLog(tmp_path / "events.log") as log:
            log.append(first)

            # when a record that is not the next one is appended
            # then it raises, whether it repeats a seq or skips one
            with pytest.raises(ValueError, match="seq"):
                log.append(first)
            with pytest.raises(ValueError, match="seq"):
                log.append(third)

            # and nothing was written: the next record is still seq 2
            assert log.next_seq == 2
            assert list(log.read_records()) == [first]
            log.append(second)
            assert list(log.read_records()) == [first, second]

    def test_the_first_record_must_have_seq_1(self, tmp_path: Path) -> None:
        # given an empty log and a record with seq 2
        record = three_records()[1]
        # when it is appended
        # then it raises
        with (
            FileLog(tmp_path / "events.log") as log,
            pytest.raises(ValueError, match="seq"),
        ):
            log.append(record)


class TestReopen:
    def test_reopening_continues_at_the_next_seq(self, tmp_path: Path) -> None:
        # given a log that held two records and was closed
        path = tmp_path / "events.log"
        first, second, third = three_records()
        with FileLog(path) as log:
            log.append(first)
            log.append(second)

        # when it is opened again
        with FileLog(path) as reopened:
            # then it expects seq 3, and an append at seq 3 is accepted
            assert reopened.next_seq == 3
            reopened.append(third)

            # and all three records read back
            assert list(reopened.read_records()) == [first, second, third]

    def test_a_reopened_log_refuses_a_repeated_seq(self, tmp_path: Path) -> None:
        # given a log that held one record and was closed
        path = tmp_path / "events.log"
        first = three_records()[0]
        with FileLog(path) as log:
            log.append(first)

        # when the same record is appended after reopening
        # then it raises
        with FileLog(path) as reopened, pytest.raises(ValueError, match="seq"):
            reopened.append(first)

    def test_reopening_an_empty_log_starts_at_seq_1(self, tmp_path: Path) -> None:
        # given a log that was created and closed without any record
        path = tmp_path / "events.log"
        FileLog(path).close()

        # when it is opened again
        with FileLog(path) as reopened:
            # then the first record must have seq 1
            assert reopened.next_seq == 1


class TestHeader:
    @pytest.mark.parametrize(
        "header",
        [
            pytest.param(b"NOTALOG\x01", id="wrong magic"),
            pytest.param(b"OBEVLOG\x02", id="wrong version"),
            pytest.param(b"OBEVLOG", id="header cut short"),
            pytest.param(b"x", id="one byte"),
        ],
    )
    def test_opening_a_file_with_a_bad_header_raises(
        self, tmp_path: Path, header: bytes
    ) -> None:
        # given a file that does not start with this log's header
        path = tmp_path / "events.log"
        path.write_bytes(header)

        # when a log is opened on it
        # then it raises, and the file is left as it was
        with pytest.raises(LogFormatError):
            FileLog(path)
        assert path.read_bytes() == header


class TestFsync:
    def test_every_append_is_flushed_to_disk_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given fsync is counted
        calls: list[int] = []
        monkeypatch.setattr(os, "fsync", calls.append)

        # when three records are appended to a log opened with the default
        with FileLog(tmp_path / "events.log") as log:
            before = len(calls)
            for record in three_records():
                log.append(record)

        # then each append called fsync once
        assert len(calls) - before == 3

    def test_fsync_false_never_calls_fsync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given fsync is counted
        calls: list[int] = []
        monkeypatch.setattr(os, "fsync", calls.append)

        # when records are appended to a log opened with fsync=False
        with FileLog(tmp_path / "events.log", fsync=False) as log:
            for record in three_records():
                log.append(record)

            # then fsync is never called, and the records are still readable
            assert list(log.read_records()) == three_records()
        assert calls == []


class TestIntactFileIsRequired:
    def test_a_flipped_payload_byte_fails_its_checksum(self, tmp_path: Path) -> None:
        # given a log with one record, whose last byte is then changed
        path = tmp_path / "events.log"
        with FileLog(path) as log:
            log.append(three_records()[0])
        data = bytearray(path.read_bytes())
        data[-1] ^= 0x01
        path.write_bytes(data)

        # when it is opened
        # then it raises
        with pytest.raises(LogFormatError, match="checksum"):
            FileLog(path)

    def test_a_record_cut_short_raises(self, tmp_path: Path) -> None:
        # given a log with one record, with its last byte removed
        path = tmp_path / "events.log"
        with FileLog(path) as log:
            log.append(three_records()[0])
        path.write_bytes(path.read_bytes()[:-1])

        # when it is opened
        # then it raises, and the file is not repaired
        with pytest.raises(LogFormatError, match="cut short"):
            FileLog(path)
        assert len(path.read_bytes()) > len(HEADER)

    def test_a_cut_short_frame_header_raises(self, tmp_path: Path) -> None:
        # given a file holding the header and three bytes of a frame
        path = tmp_path / "events.log"
        path.write_bytes(HEADER + b"\x00\x00\x00")

        # when it is opened
        # then it raises
        with pytest.raises(LogFormatError, match="cut short"):
            FileLog(path)

    def test_a_record_whose_seq_is_not_its_position_raises(
        self, tmp_path: Path
    ) -> None:
        # given a file whose first record carries seq 2
        path = tmp_path / "events.log"
        path.write_bytes(HEADER + frame(encode_record(three_records()[1])))

        # when it is opened
        # then it raises
        with pytest.raises(LogFormatError, match="seq"):
            FileLog(path)

    def test_a_valid_frame_around_an_invalid_record_raises(
        self, tmp_path: Path
    ) -> None:
        # given a file whose first frame has a good checksum but a payload that
        # is not a record
        path = tmp_path / "events.log"
        path.write_bytes(HEADER + frame(b"not json"))

        # when it is opened
        # then the codec's error surfaces
        with pytest.raises(LogFormatError):
            FileLog(path)
