"""An event log that keeps its records in memory, for tests.

It follows the `EventLog` contract and the seq rules of `FileLog`, and can be
told to fail at a chosen seq, so that a test can drive the sequencer through a
failed append without touching a disk.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Literal, Self

from orderbook_engine.app.event_log import LogFailedError
from orderbook_engine.app.log_record import LogRecord


class InMemoryLog:
    """Stores records in a list.

    With `fail_at_seq` set, the append of the record with that seq raises
    `OSError` and the log is then failed: every later append raises
    `LogFailedError`. `fail_mode` says what the failed append leaves behind.
    With "before" the record is not stored. With "after" it is stored and the
    error is raised afterwards, like an fsync that fails after a successful
    write.
    """

    def __init__(
        self,
        *,
        fail_at_seq: int | None = None,
        fail_mode: Literal["before", "after"] = "before",
    ) -> None:
        if fail_at_seq is not None and fail_at_seq < 1:
            raise ValueError(f"fail_at_seq must be at least 1, got {fail_at_seq}")
        self._fail_at_seq = fail_at_seq
        self._fail_mode = fail_mode
        self._records: list[LogRecord] = []
        self._failed = False

    @classmethod
    def from_records(
        cls,
        records: Iterable[LogRecord],
        *,
        fail_at_seq: int | None = None,
        fail_mode: Literal["before", "after"] = "before",
    ) -> Self:
        """A fresh, non-failed log that already holds `records`.

        Stands in for reopening a log: the records follow the usual seq rules
        and are never subject to the failure injection, which applies only to
        appends made afterwards. Raises `ValueError` if a seq is wrong.
        """
        log = cls(fail_at_seq=fail_at_seq, fail_mode=fail_mode)
        for record in records:
            log._check_seq(record)
            log._records.append(record)
        return log

    def append(self, record: LogRecord) -> None:
        """Store `record`. Its seq must be the number of stored records plus 1.

        Raises `ValueError`, storing nothing, if the seq is wrong.
        """
        if self._failed:
            raise LogFailedError("an earlier append failed")
        self._check_seq(record)
        if record.seq == self._fail_at_seq:
            self._failed = True
            if self._fail_mode == "after":
                self._records.append(record)
            raise OSError(f"injected failure at seq {record.seq}")
        self._records.append(record)

    def read_records(self) -> Iterator[LogRecord]:
        """The stored records, from the first."""
        return iter(list(self._records))

    def _check_seq(self, record: LogRecord) -> None:
        expected_seq = len(self._records) + 1
        if record.seq != expected_seq:
            raise ValueError(f"record seq must be {expected_seq}, got {record.seq}")
