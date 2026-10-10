"""Rebuild the book from the records of a log, and check the log against it.

Recovery runs each recorded command through the engine again, with the seq the
record carries, and compares the events the engine produces with the events
the record holds. They are equal unless the engine is no longer deterministic,
or the log was changed, so a difference is reported and never repaired. This
module is pure: it reads records from any iterable and does no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.domain.events import Event
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.replay.replay import apply_command


class LogMismatchError(Exception):
    """Re-running a recorded command gave other events than the log holds."""

    def __init__(
        self, seq: int, expected: tuple[Event, ...], actual: tuple[Event, ...]
    ) -> None:
        super().__init__(
            f"record {seq}: the log holds {expected!r}, the engine produced {actual!r}"
        )
        self.seq = seq
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class RecoveredBook:
    """The book after the recorded commands, and the seq the next command gets."""

    book: OrderBook
    next_seq: int


def recover_book(records: Iterable[LogRecord]) -> RecoveredBook:
    """Apply `records` in order to a fresh book, checking each one's events.

    The records must be those of one log, in order, as `FileLog` reads them.
    """
    book = OrderBook()
    next_seq = 1
    for record in records:
        actual = tuple(apply_command(book, record.command, record.seq))
        if actual != record.events:
            raise LogMismatchError(record.seq, record.events, actual)
        next_seq = record.seq + 1
    return RecoveredBook(book=book, next_seq=next_seq)
