"""An append-only log of records in a single file.

File layout. The file starts with an 8-byte header: the 7 magic bytes
`OBEVLOG` and one version byte (currently 1). Records follow, one per command,
and the record number is the command's seq: the first record has seq 1.

Each record is framed as

    4 bytes  length of the payload, big-endian unsigned
    4 bytes  CRC32 over the 4 length bytes followed by the payload, big-endian
    n bytes  payload: the record encoded by `encode_record`

A payload is at most `MAX_PAYLOAD_SIZE` bytes. `append` refuses a larger one,
and a frame on disk that declares a larger length is corruption.

Recovery. Opening a file checks it from the start, and repairs only damage that
a crash while appending can cause, in this order:

1. A file that is empty, or only the first bytes of the header, is started
   again with a fresh header.
2. A final record that is cut short (its frame or payload is incomplete) is
   truncated away.
3. A final record that fails its checksum, with nothing after it, is
   truncated away.
4. Anything else is corruption and raises `CorruptLogError` with the byte
   offset, leaving the file untouched: a bad record followed by more data, a
   seq that is not the previous one plus 1, a payload that cannot be decoded
   although its checksum matches, a declared length over `MAX_PAYLOAD_SIZE`,
   or a header that is not ours.

Known limitation. A length that is damaged to a value under the maximum, so
that the record seems to run past the end of the file, cannot be told apart
from a cut-short record. It is truncated like one, together with every record
after it. `dropped_bytes` reports how much recovery removed, so that the caller
can log it.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from orderbook_engine.app.event_log import CorruptLogError, LogFailedError
from orderbook_engine.app.log_record import (
    LogFormatError,
    LogRecord,
    decode_record,
    encode_record,
)

_MAGIC = b"OBEVLOG"
_VERSION = 1
_HEADER = _MAGIC + bytes([_VERSION])
_FRAME = struct.Struct(">II")  # payload length, CRC32 of length bytes + payload

MAX_PAYLOAD_SIZE = 1024 * 1024  # bytes; a larger length on disk is not a torn write


class _TornTail(Exception):
    """The file ends in a record that was not completely written."""

    def __init__(self, offset: int) -> None:
        super().__init__(offset)
        self.offset = offset


class FileLog:
    """Appends records to a file and reads them back.

    With `fsync=True` (the default) every append is flushed to disk before it
    returns. Pass `fsync=False` only where durability does not matter.
    """

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self._path = path
        self._fsync = fsync
        self._failed = False
        self._dropped_bytes = 0
        self._next_seq = 1

        created = not path.exists()
        size = 0 if created else path.stat().st_size
        if size < len(_HEADER) and _HEADER.startswith(
            b"" if size == 0 else path.read_bytes()
        ):
            self._dropped_bytes = size
            self._start_again(created=created)
        else:
            self._next_seq = self._recover() + 1
        self._file: BinaryIO = path.open("ab")

    @property
    def next_seq(self) -> int:
        """The seq the next appended record must carry."""
        return self._next_seq

    @property
    def dropped_bytes(self) -> int:
        """How many bytes opening the log removed from the file."""
        return self._dropped_bytes

    def append(self, record: LogRecord) -> None:
        """Write `record` and flush it. Its seq must be `next_seq`.

        Raises `ValueError`, before writing anything, if the seq is not
        `next_seq` or the encoded record is over `MAX_PAYLOAD_SIZE` bytes.

        If writing fails, the error is raised and the log refuses further
        appends until it is reopened, because the file may now end in a
        half-written record.
        """
        if self._failed:
            raise LogFailedError("an earlier append failed; reopen the log")
        if record.seq != self._next_seq:
            raise ValueError(f"record seq must be {self._next_seq}, got {record.seq}")
        payload = encode_record(record)
        if len(payload) > MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"encoded record is {len(payload)} bytes, "
                f"over the limit of {MAX_PAYLOAD_SIZE}"
            )
        length = struct.pack(">I", len(payload))
        crc = zlib.crc32(length + payload)
        try:
            self._file.write(length + struct.pack(">I", crc) + payload)
            self._sync(self._file)
        except OSError:
            self._failed = True
            raise
        self._next_seq += 1

    def read_records(self) -> Iterator[LogRecord]:
        """The records, from the first."""
        try:
            for _, record in _scan(self._path):
                yield record
        except _TornTail as torn:
            raise CorruptLogError(
                torn.offset, f"{self._path} ends in an incomplete record"
            ) from None

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _start_again(self, *, created: bool) -> None:
        """Write a fresh header over whatever is there."""
        with self._path.open("wb") as file:
            file.write(_HEADER)
            self._sync(file)
        if created and self._fsync:
            # Without this, the new file's name may not survive a crash.
            _fsync_directory(self._path.parent)

    def _recover(self) -> int:
        """Check the file and truncate a torn tail. Returns the record count."""
        count = 0
        try:
            for _ in _scan(self._path):
                count += 1
        except _TornTail as torn:
            self._dropped_bytes = self._path.stat().st_size - torn.offset
            with self._path.open("r+b") as file:
                file.truncate(torn.offset)
                self._sync(file)
        return count

    def _sync(self, file: BinaryIO) -> None:
        file.flush()
        if self._fsync:
            os.fsync(file.fileno())


def _scan(path: Path) -> Iterator[tuple[int, LogRecord]]:
    """Walk the records, yielding each with the offset where it ends.

    Raises `_TornTail` at a repairable damaged end and `CorruptLogError` at
    any other damage.
    """
    size = path.stat().st_size
    with path.open("rb") as file:
        if file.read(len(_HEADER)) != _HEADER:
            raise CorruptLogError(0, f"{path} is not an event log of this version")
        offset = len(_HEADER)
        expected_seq = 1
        while offset < size:
            frame = file.read(_FRAME.size)
            if len(frame) < _FRAME.size:
                raise _TornTail(offset)
            length, crc = _FRAME.unpack(frame)
            if length > MAX_PAYLOAD_SIZE:
                raise CorruptLogError(
                    offset,
                    f"{path}: a record declares {length} bytes, "
                    f"over the limit of {MAX_PAYLOAD_SIZE}",
                )
            end = offset + _FRAME.size + length
            if end > size:
                raise _TornTail(offset)
            payload = file.read(length)
            if zlib.crc32(frame[:4] + payload) != crc:
                if end == size:
                    raise _TornTail(offset)
                raise CorruptLogError(offset, f"{path}: a record fails its checksum")
            try:
                record = decode_record(payload)
            except LogFormatError as error:
                raise CorruptLogError(
                    offset, f"{path}: a record cannot be decoded: {error}"
                ) from error
            if record.seq != expected_seq:
                raise CorruptLogError(
                    offset,
                    f"{path}: expected seq {expected_seq}, found {record.seq}",
                )
            yield end, record
            offset = end
            expected_seq += 1


def _fsync_directory(directory: Path) -> None:
    """Flush a directory's entries to disk, where the platform allows it."""
    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
