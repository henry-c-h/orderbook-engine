"""What the sequencer needs from an event log.

`EventLog` is the interface; `FileLog` (adapters) and `InMemoryLog` implement
it. This module also holds the errors that every implementation raises.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from orderbook_engine.app.log_record import LogRecord


class CorruptLogError(Exception):
    """The file is damaged in a way that recovery does not repair.

    `offset` is the byte offset of the first bad record, or 0 for the header.
    """

    def __init__(self, offset: int, message: str) -> None:
        super().__init__(f"corrupt log at byte {offset}: {message}")
        self.offset = offset


class LogFailedError(Exception):
    """An earlier append failed, so the log refuses more appends.

    The failed record may or may not have been stored. The log stays failed
    until it is replaced by a fresh instance, for example one reopened from its
    file, which repairs a half-written record, or one rebuilt from its stored
    records.
    """


class EventLog(Protocol):
    """An append-only log of records, numbered from 1 by seq."""

    def append(self, record: LogRecord) -> None:
        """Store `record`. Its seq must be 1 for the first record and the
        previous record's seq plus 1 after that, otherwise `ValueError` is
        raised and nothing is stored.

        May raise `OSError` if the write fails, or `LogFailedError` if an
        earlier append failed. After a failure the log refuses further
        appends until it is replaced by a fresh instance, and the failed
        record may or may not have been stored.
        """
        ...

    def read_records(self) -> Iterator[LogRecord]:
        """The stored records, from the first."""
        ...
