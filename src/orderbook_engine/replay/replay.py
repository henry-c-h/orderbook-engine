from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import assert_never

from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit
from orderbook_engine.domain.events import Event
from orderbook_engine.domain.models import is_pos_int
from orderbook_engine.engine.order_book import OrderBook


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What a replay produced: every event in order, and the final book."""

    events: list[Event]
    book: OrderBook


def apply_command(book: OrderBook, command: Command, seq: int) -> list[Event]:
    """Run one command on `book`. A cancel does not use `seq`."""
    match command:
        case Submit(order=order):
            return book.submit(order, seq)
        case Cancel(order_id=order_id):
            return book.cancel(order_id)
        case Amend(order_id=order_id, price=price, quantity=quantity):
            return book.amend(order_id, price, quantity, seq)
        case _:
            assert_never(command)


def replay(commands: Iterable[Command], *, start_seq: int = 1) -> ReplayResult:
    """Apply `commands` in order to a fresh book.

    Every command consumes the next sequence number, including cancels and
    commands that end up rejected, so a command's number depends only on its
    position in the log. Numbering starts at `start_seq`.
    """
    if not is_pos_int(start_seq):
        raise ValueError(f"start_seq must be a positive int, got {start_seq!r}")

    book = OrderBook()
    events: list[Event] = []
    for seq, command in enumerate(commands, start=start_seq):
        events.extend(apply_command(book, command, seq))
    return ReplayResult(events=events, book=book)
