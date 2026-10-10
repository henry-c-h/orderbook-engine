import io
import os
import struct
import zlib
from collections.abc import Buffer
from dataclasses import replace
from pathlib import Path

import pytest
from builders import buy, sell
from strategies import records_for

from orderbook_engine.adapters import file_log
from orderbook_engine.adapters.file_log import (
    MAX_PAYLOAD_SIZE,
    CorruptLogError,
    FileLog,
    LogFailedError,
)
from orderbook_engine.app.log_record import (
    LogRecord,
    decode_record,
    encode_record,
)
from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit
from orderbook_engine.domain.events import Rejected, RejectReason

HEADER = b"OBEVLOG\x01"


def six_commands() -> list[Command]:
    """A rest, a partial fill, another rest, a cancel, a rejected cancel, an amend."""
    return [
        Submit(buy("b1", 100, participant_id="p1")),
        Submit(sell("s1", 100, quantity=4, participant_id="p2")),
        Submit(buy("b2", 99, participant_id="p3")),
        Cancel("b2"),
        Cancel("nope"),
        Amend("b1", 98, 12),
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


def record_with_payload_size(size: int) -> LogRecord:
    """A record with seq 1 whose encoding is exactly `size` bytes long."""

    def record(command_id_length: int) -> LogRecord:
        return LogRecord(
            seq=1,
            command=Cancel("c" * command_id_length),
            events=(Rejected("e", RejectReason.UNKNOWN_ORDER),),
        )

    # Every character of the command's id adds one byte to the encoding.
    base = len(encode_record(record(1)))
    result = record(1 + size - base)
    assert len(encode_record(result)) == size
    return result


def frame(payload: bytes) -> bytes:
    """A record frame, built here independently of the code under test."""
    length = struct.pack(">I", len(payload))
    return length + struct.pack(">I", zlib.crc32(length + payload)) + payload


def file_bytes(records: list[LogRecord]) -> bytes:
    """The bytes of a log file holding `records`."""
    return HEADER + b"".join(frame(encode_record(record)) for record in records)


def record_ends(records: list[LogRecord]) -> list[int]:
    """The offset at which each record's frame ends in the file."""
    ends: list[int] = []
    offset = len(HEADER)
    for record in records:
        offset += len(frame(encode_record(record)))
        ends.append(offset)
    return ends


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
            assert log.dropped_bytes == 0

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

    def test_a_record_over_the_maximum_payload_size_is_refused_before_writing(
        self, tmp_path: Path
    ) -> None:
        # given a log and a record whose encoding is one byte over the maximum
        path = tmp_path / "events.log"
        too_big = record_with_payload_size(MAX_PAYLOAD_SIZE + 1)
        with FileLog(path) as log:
            # when it is appended
            # then it raises, and nothing is written
            with pytest.raises(ValueError, match="limit"):
                log.append(too_big)
            assert path.read_bytes() == HEADER
            assert log.next_seq == 1

            # and the log is not marked failed: the next record is accepted
            log.append(three_records()[0])
            assert log.next_seq == 2

    def test_a_record_of_exactly_the_maximum_payload_size_is_accepted(
        self, tmp_path: Path
    ) -> None:
        # given a record whose encoding is exactly the maximum
        path = tmp_path / "events.log"
        biggest = record_with_payload_size(MAX_PAYLOAD_SIZE)

        # when it is appended, and the log is closed and opened again
        with FileLog(path, fsync=False) as log:
            log.append(biggest)
        with FileLog(path, fsync=False) as reopened:
            # then it reads back
            assert list(reopened.read_records()) == [biggest]

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
            assert reopened.dropped_bytes == 0
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
        "content",
        [
            pytest.param(b"NOTALOG\x01", id="wrong magic"),
            pytest.param(b"OBEVLOG\x02", id="wrong version"),
            pytest.param(b"OBEVLOX", id="seven bytes that are not a header prefix"),
            pytest.param(b"x", id="one byte that is not a header prefix"),
            pytest.param(b"not an event log at all, just some text", id="other text"),
        ],
    )
    def test_a_file_that_is_not_this_log_raises_and_is_left_alone(
        self, tmp_path: Path, content: bytes
    ) -> None:
        # given a file that does not start with this log's header
        path = tmp_path / "events.log"
        path.write_bytes(content)

        # when a log is opened on it
        # then it raises at offset 0, and the file is left as it was
        with pytest.raises(CorruptLogError) as raised:
            FileLog(path)
        assert raised.value.offset == 0
        assert path.read_bytes() == content

    @pytest.mark.parametrize("length", range(len(HEADER)))
    def test_a_file_holding_only_the_start_of_the_header_is_started_again(
        self, tmp_path: Path, length: int
    ) -> None:
        # given a file cut off inside the header, as a crash during creation
        # would leave it (zero bytes up to seven)
        path = tmp_path / "events.log"
        path.write_bytes(HEADER[:length])

        # when a log is opened on it
        with FileLog(path) as log:
            # then the header is written afresh and the log is empty
            assert path.read_bytes() == HEADER
            assert list(log.read_records()) == []
            assert log.next_seq == 1
            assert log.dropped_bytes == length

    def test_a_file_with_only_the_header_is_an_empty_log(self, tmp_path: Path) -> None:
        # given a file holding exactly the header
        path = tmp_path / "events.log"
        path.write_bytes(HEADER)

        # when a log is opened on it
        with FileLog(path) as log:
            # then it is empty, nothing was dropped and the file is unchanged
            assert list(log.read_records()) == []
            assert log.next_seq == 1
            assert log.dropped_bytes == 0
        assert path.read_bytes() == HEADER


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

    @pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX only")
    def test_creating_a_file_fsyncs_its_directory_but_reopening_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given fsync records which inode it was called on
        inodes: list[int] = []

        def record_inode(descriptor: int) -> None:
            inodes.append(os.fstat(descriptor).st_ino)

        monkeypatch.setattr(os, "fsync", record_inode)
        directory_inode = tmp_path.stat().st_ino
        path = tmp_path / "events.log"

        # when a new log file is created
        FileLog(path).close()

        # then the directory was fsynced
        assert directory_inode in inodes

        # and when the file is opened again, the directory is not fsynced
        inodes.clear()
        FileLog(path).close()
        assert directory_inode not in inodes

    def test_fsync_false_does_not_fsync_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given fsync is counted
        calls: list[int] = []
        monkeypatch.setattr(os, "fsync", calls.append)

        # when a new log file is created with fsync=False
        FileLog(tmp_path / "events.log", fsync=False).close()

        # then nothing was fsynced
        assert calls == []


class TestFailedAppend:
    def test_after_an_io_error_the_log_refuses_appends_until_reopened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given a log whose next fsync fails
        path = tmp_path / "events.log"
        records = records_for(six_commands())
        log = FileLog(path)

        def failing_fsync(descriptor: int) -> None:
            raise OSError("disk full")

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", failing_fsync)

            # when an append hits the error
            # then the error is raised
            with pytest.raises(OSError, match="disk full"):
                log.append(records[0])

        # and the disk works again, but the log refuses further appends
        with pytest.raises(LogFailedError):
            log.append(records[0])
        assert log.next_seq == 1
        log.close()

        # when the log is reopened
        with FileLog(path) as reopened:
            # then it learns what reached the file, and appends again from there
            present = list(reopened.read_records())
            assert present == records[: len(present)]
            assert reopened.next_seq == len(present) + 1
            reopened.append(records[len(present)])


class HalfWriter(io.BufferedWriter):
    """A file handle that writes the first half of what it is given, then fails."""

    def write(self, buffer: Buffer, /) -> int:
        data = bytes(buffer)
        super().write(data[: len(data) // 2])
        self.flush()
        raise OSError("disk full")


class TestPartialWrite:
    def test_a_half_written_record_is_dropped_when_the_log_is_reopened(
        self, tmp_path: Path
    ) -> None:
        # given a log holding one record
        path = tmp_path / "events.log"
        records = records_for(six_commands())
        log = FileLog(path, fsync=False)
        log.append(records[0])
        size_after_first = path.stat().st_size

        # and its file handle is swapped for one that writes half a frame and
        # then fails (the only private access in these tests, to simulate a
        # write that dies partway)
        log._file.close()  # pyright: ignore[reportPrivateUsage]
        log._file = HalfWriter(io.FileIO(path, "ab"))  # pyright: ignore[reportPrivateUsage]

        # when the next append fails partway
        with pytest.raises(OSError, match="disk full"):
            log.append(records[1])

        # then half a record is really on disk
        size_with_torn_tail = path.stat().st_size
        assert size_with_torn_tail > size_after_first

        # and the log refuses further appends and did not count the record
        with pytest.raises(LogFailedError):
            log.append(records[1])
        assert log.next_seq == 2
        log.close()

        # when the log is reopened
        with FileLog(path, fsync=False) as reopened:
            # then the half record is truncated away and reported
            assert list(reopened.read_records()) == records[:1]
            assert path.stat().st_size == size_after_first
            assert reopened.dropped_bytes == size_with_torn_tail - size_after_first

            # and the record can be appended again
            reopened.append(records[1])
            assert list(reopened.read_records()) == records[:2]

    def test_an_encoding_failure_writes_nothing_and_does_not_fail_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given a log whose record encoder fails
        path = tmp_path / "events.log"
        record = three_records()[0]
        log = FileLog(path)

        def failing_encoder(record: LogRecord) -> bytes:
            raise RuntimeError("cannot encode")

        with monkeypatch.context() as patch:
            patch.setattr(file_log, "encode_record", failing_encoder)

            # when a record is appended
            # then the error is raised before any byte reaches the file
            with pytest.raises(RuntimeError, match="cannot encode"):
                log.append(record)
        assert path.read_bytes() == HEADER

        # and the log is not marked failed: the same record can be appended
        log.append(record)
        assert list(log.read_records()) == [record]
        log.close()


class TestRecovery:
    def test_cutting_the_file_at_any_byte_keeps_the_complete_records(
        self, tmp_path: Path
    ) -> None:
        # given a log of five records, and a sixth that was never written
        records = records_for(six_commands())
        written = records[:5]
        data = file_bytes(written)
        ends = record_ends(written)
        path = tmp_path / "events.log"

        # when the file is cut at every byte from the end of the header to the
        # full length, and opened
        for cut in range(len(HEADER), len(data) + 1):
            path.write_bytes(data[:cut])
            complete = sum(1 for end in ends if end <= cut)
            boundary = ends[complete - 1] if complete else len(HEADER)

            with FileLog(path, fsync=False) as log:
                # then exactly the records complete before the cut are returned
                assert list(log.read_records()) == records[:complete], f"cut {cut}"
                assert log.next_seq == complete + 1, f"cut {cut}"

                # the file is truncated to that record boundary, and the
                # dropped bytes say how much that removed
                assert path.stat().st_size == boundary, f"cut {cut}"
                assert log.dropped_bytes == cut - boundary, f"cut {cut}"

                # and the next record can be appended and read back
                log.append(records[complete])
                assert list(log.read_records()) == records[: complete + 1]

    def test_a_last_record_that_fails_its_checksum_is_truncated_away(
        self, tmp_path: Path
    ) -> None:
        # given a log of three records whose last payload byte is changed
        records = three_records()
        data = bytearray(file_bytes(records))
        data[-1] ^= 0x01
        ends = record_ends(records)
        path = tmp_path / "events.log"
        path.write_bytes(data)

        # when the log is opened
        with FileLog(path) as log:
            # then the last record is dropped and the file ends where it began
            assert list(log.read_records()) == records[:2]
            assert path.stat().st_size == ends[1]
            assert log.dropped_bytes == len(data) - ends[1]
            assert log.next_seq == 3

            # and the record can be appended again
            log.append(records[2])
            assert list(log.read_records()) == records

    def test_a_middle_record_that_fails_its_checksum_is_corruption(
        self, tmp_path: Path
    ) -> None:
        # given a log of three records whose second record has a changed byte
        records = three_records()
        data = bytearray(file_bytes(records))
        ends = record_ends(records)
        data[ends[0] + 8 + 1] ^= 0x01
        path = tmp_path / "events.log"
        path.write_bytes(data)

        # when the log is opened
        # then it raises with the offset of the bad record, and the file is
        # exactly as it was
        with pytest.raises(CorruptLogError) as raised:
            FileLog(path)
        assert raised.value.offset == ends[0]
        assert path.read_bytes() == bytes(data)

    def test_a_gap_in_the_seq_numbers_is_corruption(self, tmp_path: Path) -> None:
        # given a log whose third record carries seq 4
        first, second, third = three_records()
        records = [first, second, replace(third, seq=4)]
        data = file_bytes(records)
        ends = record_ends(records)
        path = tmp_path / "events.log"
        path.write_bytes(data)

        # when the log is opened
        # then it raises at the third record, and the file is untouched
        with pytest.raises(CorruptLogError) as raised:
            FileLog(path)
        assert raised.value.offset == ends[1]
        assert path.read_bytes() == data

    def test_a_first_record_that_is_not_seq_1_is_corruption(
        self, tmp_path: Path
    ) -> None:
        # given a log whose first record carries seq 2
        data = file_bytes([three_records()[1]])
        path = tmp_path / "events.log"
        path.write_bytes(data)

        # when the log is opened
        # then it raises at the first record, and the file is untouched
        with pytest.raises(CorruptLogError) as raised:
            FileLog(path)
        assert raised.value.offset == len(HEADER)
        assert path.read_bytes() == data

    def test_a_good_checksum_around_an_undecodable_payload_is_corruption(
        self, tmp_path: Path
    ) -> None:
        # given a log whose last frame has a good checksum but a payload that
        # is not a record
        first = three_records()[0]
        data = file_bytes([first]) + frame(b"not json")
        path = tmp_path / "events.log"
        path.write_bytes(data)

        # when the log is opened
        # then it raises at that frame, though it is the last one, and the
        # file is untouched
        with pytest.raises(CorruptLogError) as raised:
            FileLog(path)
        assert raised.value.offset == record_ends([first])[0]
        assert path.read_bytes() == data

    def test_a_length_over_the_maximum_is_corruption_not_a_torn_tail(
        self, tmp_path: Path
    ) -> None:
        # given a log of three records whose second record's length is damaged
        # to a value over the maximum payload size
        records = three_records()
        data = bytearray(file_bytes(records))
        ends = record_ends(records)
        data[ends[0] : ends[0] + 4] = struct.pack(">I", MAX_PAYLOAD_SIZE + 1)
        path = tmp_path / "events.log"
        path.write_bytes(data)

        # when the log is opened
        # then it raises at that record, and the file is untouched, so the
        # valid records after it are not lost
        with pytest.raises(CorruptLogError) as raised:
            FileLog(path)
        assert raised.value.offset == ends[0]
        assert path.read_bytes() == bytes(data)

    def test_a_length_under_the_maximum_that_runs_past_the_end_is_truncated(
        self, tmp_path: Path
    ) -> None:
        # given a log of three records whose second record's length is damaged
        # to a value under the maximum that reaches beyond the end of the file
        records = three_records()
        data = bytearray(file_bytes(records))
        ends = record_ends(records)
        damaged = len(data) + 1
        assert damaged <= MAX_PAYLOAD_SIZE
        data[ends[0] : ends[0] + 4] = struct.pack(">I", damaged)
        path = tmp_path / "events.log"
        path.write_bytes(data)

        # when the log is opened
        with FileLog(path) as log:
            # then it is truncated like a cut-short record, and the second and
            # third records are lost with it. This is a KNOWN LIMITATION: such a
            # damaged length cannot be told apart from a torn write, and
            # dropped_bytes is how the caller finds out what was removed.
            assert list(log.read_records()) == records[:1]
            assert log.dropped_bytes == len(data) - ends[0]
