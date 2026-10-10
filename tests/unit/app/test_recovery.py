from dataclasses import replace

import pytest
from builders import buy, sell
from helpers import book_state
from strategies import records_for

from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.app.recovery import LogMismatchError, recover_book
from orderbook_engine.domain.commands import Cancel, Command, Submit
from orderbook_engine.domain.events import Accepted
from orderbook_engine.domain.models import Trade
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.replay.checksum import state_checksum
from orderbook_engine.replay.replay import replay


def commands() -> list[Command]:
    """A rest, a partial fill, another rest and a cancel."""
    return [
        Submit(buy("b1", 100, participant_id="p1")),
        Submit(sell("s1", 100, quantity=4, participant_id="p2")),
        Submit(buy("b2", 99, participant_id="p3")),
        Cancel("b2"),
    ]


class TestRecoverBook:
    def test_no_records_give_an_empty_book_and_seq_1(self) -> None:
        # given no records
        # when the book is recovered
        recovered = recover_book([])

        # then the book is empty and the next command gets seq 1
        assert book_state(recovered.book) == book_state(OrderBook())
        assert recovered.next_seq == 1

    def test_the_book_equals_the_one_replay_builds(self) -> None:
        # given the records of some commands
        records = records_for(commands())

        # when the book is recovered
        recovered = recover_book(records)

        # then it has the same state as replaying the commands
        assert state_checksum(recovered.book.snapshot()) == state_checksum(
            replay(commands()).book.snapshot()
        )

    def test_the_next_seq_follows_the_last_record(self) -> None:
        # given the records of four commands
        records = records_for(commands())

        # when the book is recovered
        recovered = recover_book(records)

        # then the next command gets seq 5
        assert recovered.next_seq == 5

    def test_accepts_any_iterable_of_records(self) -> None:
        # given records held in a generator
        records = (record for record in records_for(commands()))

        # when the book is recovered
        recovered = recover_book(records)

        # then every record was used
        assert recovered.next_seq == 5

    def test_recovered_book_accepts_the_next_command(self) -> None:
        # given a recovered book
        recovered = recover_book(records_for(commands()))

        # when the next command is applied with the next seq
        accepted = recovered.book.submit(buy("b3", 98), recovered.next_seq)[0]

        # then the book carries on: seq 5 is accepted and the trade counter
        # continues after the one trade already made
        assert isinstance(accepted, Accepted)
        assert accepted.seq == 5
        assert recovered.book.snapshot().next_trade_id == 2


class TestLogMismatch:
    def tampered(self, records: list[LogRecord], index: int) -> list[LogRecord]:
        """`records`, with the events of record `index` changed."""
        record = records[index]
        changed = tuple(
            replace(event, quantity=event.quantity + 1)
            if isinstance(event, Trade)
            else event
            for event in record.events
        )
        return [
            *records[:index],
            replace(record, events=changed),
            *records[index + 1 :],
        ]

    def test_a_changed_event_raises_with_the_seq_expected_and_actual(self) -> None:
        # given records where the trade of record 2 was changed
        records = records_for(commands())
        changed = self.tampered(records, 1)

        # when the book is recovered
        # then the mismatch names record 2, what the log holds and what the
        # engine produced
        with pytest.raises(LogMismatchError) as raised:
            recover_book(changed)
        assert raised.value.seq == 2
        assert raised.value.expected == changed[1].events
        assert raised.value.actual == records[1].events

    def test_a_missing_event_raises(self) -> None:
        # given records where the trade was dropped from record 2
        records = records_for(commands())
        trimmed = replace(records[1], events=records[1].events[:1])

        # when the book is recovered
        # then it raises for record 2
        with pytest.raises(LogMismatchError) as raised:
            recover_book([records[0], trimmed, *records[2:]])
        assert raised.value.seq == 2

    def test_a_changed_command_raises(self) -> None:
        # given records where the first command was changed to another price
        records = records_for(commands())
        other = replace(records[0], command=Submit(buy("b1", 101, participant_id="p1")))

        # when the book is recovered
        # then it raises for record 1, because the engine's events differ
        with pytest.raises(LogMismatchError) as raised:
            recover_book([other, *records[1:]])
        assert raised.value.seq == 1
