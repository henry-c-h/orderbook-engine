from typing import Any

import pytest
from builders import buy
from helpers import book_state

from orderbook_engine.domain.commands import Amend, Cancel, Submit
from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Rejected,
    RejectReason,
    Replaced,
)
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.replay.replay import apply_command, replay


class TestApplyCommand:
    def test_submit_is_applied_with_the_given_seq(self) -> None:
        # given a fresh book and a submit
        book = OrderBook()
        order = buy("o1", 100)

        # when it is applied with seq 7
        events = apply_command(book, Submit(order), 7)

        # then the order is accepted with seq 7
        assert events == [Accepted(seq=7, order=order)]

    def test_cancel_removes_the_live_order(self) -> None:
        # given a book holding o1
        book = OrderBook()
        apply_command(book, Submit(buy("o1", 100)), 1)

        # when o1 is cancelled
        events = apply_command(book, Cancel("o1"), 2)

        # then it is cancelled at the user's request and the book is empty
        assert events == [Cancelled("o1", 10, CancelReason.USER_REQUESTED)]
        assert book_state(book) == book_state(OrderBook())

    def test_amend_is_applied_with_the_given_seq(self) -> None:
        # given a book holding o1
        book = OrderBook()
        apply_command(book, Submit(buy("o1", 100)), 1)

        # when o1 is moved to another price with seq 5
        events = apply_command(book, Amend("o1", 101, 10), 5)

        # then it is replaced and takes seq 5
        assert events == [Replaced("o1", 5, 101, 10)]


class TestReplay:
    def test_empty_log_gives_no_events_and_an_empty_book(self) -> None:
        # given no commands
        # when they are replayed
        result = replay([])

        # then there are no events and the book is empty
        assert result.events == []
        assert book_state(result.book) == book_state(OrderBook())

    def test_events_of_all_commands_are_returned_in_order(self) -> None:
        # given a submit followed by a cancel of the same order
        first = buy("o1", 100)

        # when they are replayed
        result = replay([Submit(first), Cancel("o1")])

        # then the events are those of the submit, then those of the cancel
        assert result.events == [
            Accepted(seq=1, order=first),
            Cancelled("o1", 10, CancelReason.USER_REQUESTED),
        ]

    def test_returned_book_holds_the_resting_orders(self) -> None:
        # given a command that leaves one order resting
        # when it is replayed
        result = replay([Submit(buy("o1", 100))])

        # then the returned book holds that order
        assert list(book_state(result.book).orders) == ["o1"]

    def test_a_cancel_consumes_a_sequence_number(self) -> None:
        # given a submit, a cancel and another submit
        second = buy("o2", 100)
        commands = [Submit(buy("o1", 100)), Cancel("o1"), Submit(second)]

        # when they are replayed
        result = replay(commands)

        # then the third command, the second submit, has seq 3
        assert result.events[-1] == Accepted(seq=3, order=second)

    def test_a_rejected_command_consumes_a_sequence_number(self) -> None:
        # given a cancel of an unknown order, then a submit
        order = buy("o1", 100)
        commands = [Cancel("nope"), Submit(order)]

        # when they are replayed
        result = replay(commands)

        # then the cancel is rejected and the submit has seq 2
        assert result.events == [
            Rejected("nope", RejectReason.UNKNOWN_ORDER),
            Accepted(seq=2, order=order),
        ]

    def test_start_seq_sets_the_first_sequence_number(self) -> None:
        # given one submit
        order = buy("o1", 100)

        # when it is replayed from seq 10
        result = replay([Submit(order)], start_seq=10)

        # then the Accepted carries seq 10
        assert result.events == [Accepted(seq=10, order=order)]

    def test_accepts_any_iterable_of_commands(self) -> None:
        # given commands held in a generator
        order = buy("o1", 100)
        commands = (command for command in [Submit(order)])

        # when they are replayed
        result = replay(commands)

        # then the command was applied
        assert result.events == [Accepted(seq=1, order=order)]

    @pytest.mark.parametrize("start_seq", [0, -1, True, 1.5])
    def test_rejects_invalid_start_seq(self, start_seq: Any) -> None:
        # given an invalid start_seq
        # when commands are replayed with it
        # then it raises
        with pytest.raises(ValueError, match="start_seq"):
            replay([], start_seq=start_seq)
