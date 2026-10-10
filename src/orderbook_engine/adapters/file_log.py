"""An append-only log of records in a single file.

File layout. The file starts with an 8-byte header: the 7 magic bytes
`OBEVLOG` and one version byte (currently 1). Records follow, one per command,
and the record number is the command's seq: the first record has seq 1.

Each record is framed as

    4 bytes  length of the payload, big-endian unsigned
    4 bytes  CRC32 over the 4 length bytes followed by the payload, big-endian
    n bytes  payload: the record encoded by `encode_record`

Reading assumes an intact file. A bad header, a record whose checksum does not
match, a record cut short or a record whose seq is not its position raises
`LogFormatError`. Nothing is repaired or truncated.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

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


class FileLog:
    """Appends records to a file and reads them back.

    With `fsync=True` (the default) every append is flushed to disk before it
    returns. Pass `fsync=False` only where durability does not matter.
    """

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self._path = path
        self._fsync = fsync
        self._next_seq = 1
        if not path.exists() or path.stat().st_size == 0:
            self._file: BinaryIO = path.open("ab")
            self._write(_HEADER)
        else:
            # Count the records already there, so that appends continue after them.
            self._next_seq = sum(1 for _ in self.read_records()) + 1
            self._file = path.open("ab")

    @property
    def next_seq(self) -> int:
        """The seq the next appended record must carry."""
        return self._next_seq

    def append(self, record: LogRecord) -> None:
        """Write `record` and flush it. Its seq must be `next_seq`."""
        if record.seq != self._next_seq:
            raise ValueError(f"record seq must be {self._next_seq}, got {record.seq}")
        payload = encode_record(record)
        length = struct.pack(">I", len(payload))
        crc = zlib.crc32(length + payload)
        self._write(length + struct.pack(">I", crc) + payload)
        self._next_seq += 1

    def read_records(self) -> Iterator[LogRecord]:
        """The records, from the first. The file must be intact."""
        with self._path.open("rb") as file:
            if file.read(len(_HEADER)) != _HEADER:
                raise LogFormatError(
                    f"{self._path}: not an event log, or an unsupported version"
                )
            seq = 1
            while header := file.read(_FRAME.size):
                if len(header) < _FRAME.size:
                    raise LogFormatError(f"{self._path}: record {seq} is cut short")
                length, crc = _FRAME.unpack(header)
                payload = file.read(length)
                if len(payload) < length:
                    raise LogFormatError(f"{self._path}: record {seq} is cut short")
                if zlib.crc32(header[:4] + payload) != crc:
                    raise LogFormatError(
                        f"{self._path}: record {seq} fails its checksum"
                    )
                record = decode_record(payload)
                if record.seq != seq:
                    raise LogFormatError(
                        f"{self._path}: record {seq} carries seq {record.seq}"
                    )
                yield record
                seq += 1

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

    def _write(self, data: bytes) -> None:
        self._file.write(data)
        self._file.flush()
        if self._fsync:
            os.fsync(self._file.fileno())
