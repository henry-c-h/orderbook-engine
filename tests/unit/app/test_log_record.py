import dataclasses
import json
from collections.abc import Callable
from typing import Any

import pytest
from builders import buy, market_order, sell, valid_trade

from orderbook_engine.app.log_record import (
    LogFormatError,
    LogRecord,
    decode_record,
    encode_record,
)
from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit
from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Rejected,
    RejectReason,
    Replaced,
)
from orderbook_engine.domain.models import Side, TimeInForce

COMMANDS: list[Command] = [
    Submit(buy("o1", 100)),
    Submit(sell("o2", 101, time_in_force=TimeInForce.IOC, participant_id="p2")),
    Submit(market_order("o3", Side.BUY, quantity=7)),
    Cancel("o1"),
    Amend("o1", 99, 5),
]

EVENTS: list[Event] = [
    Accepted(seq=1, order=buy("o1", 100)),
    Accepted(seq=2, order=market_order("o3", Side.SELL)),
    *(Rejected("o9", reason) for reason in RejectReason),
    valid_trade(),
    *(Cancelled("o1", 4, reason) for reason in CancelReason),
    Replaced("o1", 3, 99, 5),
]


def record_of(command: Command, *events: Event, seq: int = 1) -> LogRecord:
    return LogRecord(seq=seq, command=command, events=events)


def sample_record() -> LogRecord:
    return record_of(Submit(buy("o1", 100)), Accepted(seq=1, order=buy("o1", 100)))


def mutated(change: Callable[[dict[str, Any]], None]) -> bytes:
    """The sample record's JSON after `change` was applied to it."""
    payload: dict[str, Any] = json.loads(encode_record(sample_record()))
    change(payload)
    return json.dumps(payload).encode("utf-8")


class TestLogRecord:
    def test_carries_seq_command_and_events(self) -> None:
        # given a command and the event it produced
        command = Cancel("o1")
        event = Cancelled("o1", 10, CancelReason.USER_REQUESTED)

        # when a record is built from them
        record = LogRecord(seq=4, command=command, events=(event,))

        # then it carries all three
        assert (record.seq, record.command, record.events) == (4, command, (event,))

    def test_is_immutable(self) -> None:
        # given a record
        record = sample_record()

        # when a field is assigned
        # then the record refuses
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.seq = 2  # pyright: ignore[reportAttributeAccessIssue]

    @pytest.mark.parametrize("seq", [0, -1, True, 1.5, "1", None])
    def test_rejects_invalid_seq(self, seq: Any) -> None:
        # given an invalid seq
        # when a record is built with it
        # then it raises
        with pytest.raises(ValueError, match="seq"):
            LogRecord(seq=seq, command=Cancel("o1"), events=(valid_trade(),))

    def test_rejects_no_events(self) -> None:
        # given no events
        # when a record is built from them
        # then it raises
        with pytest.raises(ValueError, match="event"):
            LogRecord(seq=1, command=Cancel("o1"), events=())


class TestEncodeRecord:
    def test_writes_compact_json_with_a_type_tag_on_command_and_events(self) -> None:
        # given a cancel and the event it produced
        record = record_of(
            Cancel("o1"), Cancelled("o1", 10, CancelReason.USER_REQUESTED)
        )

        # when it is encoded
        encoded = encode_record(record)

        # then it is the compact JSON, with the field names written out by hand
        assert encoded == (
            b'{"seq":1,"command":{"type":"Cancel","order_id":"o1"},'
            b'"events":[{"type":"Cancelled","order_id":"o1","quantity":10,'
            b'"reason":"USER_REQUESTED"}]}'
        )

    def test_writes_a_market_orders_missing_price_as_null(self) -> None:
        # given a submit of a market order
        record = record_of(
            Submit(market_order("o3", Side.BUY)),
            Rejected("o3", RejectReason.DUPLICATE_ORDER_ID),
        )

        # when it is encoded
        payload = json.loads(encode_record(record))

        # then the order's price is null
        assert payload["command"]["order"]["price"] is None

    def test_writes_non_ascii_ids_as_valid_utf_8(self) -> None:
        # given a cancel of an order whose id has non-ASCII characters
        record = record_of(
            Cancel("ordér-1 😀"), Rejected("ordér-1 😀", RejectReason.UNKNOWN_ORDER)
        )

        # when it is encoded and the bytes are read as UTF-8 text
        text = encode_record(record).decode("utf-8")

        # then the text parses to the same id
        assert json.loads(text)["command"]["order_id"] == "ordér-1 😀"


class TestRoundTrip:
    @pytest.mark.parametrize(
        "command", COMMANDS, ids=lambda command: type(command).__name__
    )
    def test_every_command_round_trips(self, command: Command) -> None:
        # given a record holding the command
        record = record_of(command, Rejected("o9", RejectReason.UNKNOWN_ORDER))

        # when it is encoded and decoded
        decoded = decode_record(encode_record(record))

        # then the result equals the original
        assert decoded == record

    @pytest.mark.parametrize("event", EVENTS, ids=lambda event: repr(event)[:60])
    def test_every_event_round_trips(self, event: Event) -> None:
        # given a record holding the event
        record = record_of(Cancel("o1"), event)

        # when it is encoded and decoded
        decoded = decode_record(encode_record(record))

        # then the result equals the original
        assert decoded == record

    def test_a_record_with_several_events_round_trips_in_order(self) -> None:
        # given a record whose command produced an accept, a trade and a cancel
        record = record_of(
            Submit(buy("o2", 100, participant_id="p2")),
            Accepted(seq=7, order=buy("o2", 100, participant_id="p2")),
            valid_trade(),
            Cancelled("o2", 3, CancelReason.SELF_TRADE_PREVENTED),
            seq=7,
        )

        # when it is encoded and decoded
        decoded = decode_record(encode_record(record))

        # then the events come back in the same order
        assert decoded == record

    def test_decoded_events_are_a_tuple(self) -> None:
        # given a decoded record
        decoded = decode_record(encode_record(sample_record()))

        # when its events are inspected
        # then they are a tuple, like the original's
        assert type(decoded.events) is tuple


class TestDecodeRecord:
    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"not json", id="not json"),
            pytest.param(b"\xff\xfe", id="not utf-8"),
            pytest.param(b"[]", id="a list, not an object"),
            pytest.param(b"null", id="null"),
            pytest.param(encode_record(sample_record()) + b"x", id="trailing bytes"),
            pytest.param(mutated(lambda payload: payload.pop("seq")), id="missing seq"),
            pytest.param(
                mutated(lambda payload: payload.update(extra=1)), id="extra field"
            ),
            pytest.param(
                mutated(lambda payload: payload.update(seq=True)), id="seq is a bool"
            ),
            pytest.param(
                mutated(lambda payload: payload.update(seq="1")), id="seq is a string"
            ),
            pytest.param(
                mutated(lambda payload: payload.update(seq=0)), id="seq is zero"
            ),
            pytest.param(
                mutated(lambda payload: payload.update(events=[])), id="no events"
            ),
            pytest.param(
                mutated(lambda payload: payload.update(events={})),
                id="events is not a list",
            ),
            pytest.param(
                mutated(lambda payload: payload["command"].update(type="Explode")),
                id="unknown command type",
            ),
            pytest.param(
                mutated(lambda payload: payload["command"].pop("type")),
                id="command without a type",
            ),
            pytest.param(
                mutated(lambda payload: payload["events"][0].update(type="Exploded")),
                id="unknown event type",
            ),
            pytest.param(
                mutated(lambda payload: payload["events"][0].update(seq=0)),
                id="event the domain rejects",
            ),
            pytest.param(
                mutated(lambda payload: payload["command"]["order"].update(quantity=0)),
                id="order the domain rejects",
            ),
            pytest.param(
                mutated(lambda payload: payload["command"]["order"].update(side="UP")),
                id="unknown enum value",
            ),
            pytest.param(
                mutated(lambda payload: payload["command"]["order"].update(order_id=5)),
                id="order id is a number",
            ),
            pytest.param(
                mutated(lambda payload: payload["command"]["order"].update(price=True)),
                id="price is a bool",
            ),
            pytest.param(
                mutated(lambda payload: payload["command"]["order"].pop("price")),
                id="order without a price field",
            ),
        ],
    )
    def test_malformed_input_raises_log_format_error(self, data: bytes) -> None:
        # given bytes that are not a valid record
        # when they are decoded
        # then one error type is raised
        with pytest.raises(LogFormatError):
            decode_record(data)

    def test_a_market_order_that_carries_a_price_is_rejected(self) -> None:
        # given a record whose market order has a price
        data = mutated(
            lambda payload: payload["command"]["order"].update(
                order_type="MARKET", time_in_force="IOC"
            )
        )

        # when it is decoded
        # then the domain's rule surfaces as a log format error
        with pytest.raises(LogFormatError, match="price"):
            decode_record(data)
