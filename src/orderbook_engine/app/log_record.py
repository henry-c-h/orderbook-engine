"""The record the event log stores for one command, and its JSON encoding.

A record holds the command, its seq and the events the engine produced for it.
The record number in the log is the command's seq. This module is pure: it
turns records into bytes and back, and does no I/O.

Encoding. A record is one compact JSON object, UTF-8 encoded:

    {"seq": 1, "command": {...}, "events": [{...}, ...]}

Every command and every event is an object with a "type" field naming its
class (`Submit`, `Accepted`, `Trade`, ...) and one field for each field of the
class. An order is a nested object without a type. Enums are written as their
string values and a market order's missing price as null. Decoding is strict:
a missing or extra field, a value of the wrong JSON type, an unknown type name
or values that the domain classes reject all raise `LogFormatError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, assert_never, cast

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
from orderbook_engine.domain.models import (
    Order,
    OrderType,
    Side,
    TimeInForce,
    Trade,
    is_pos_int,
)


class LogFormatError(Exception):
    """Bytes that are not a valid log record, or a log file that is not intact."""


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One command, its seq and the events it produced (at least one)."""

    seq: int
    command: Command
    events: tuple[Event, ...]

    def __post_init__(self) -> None:
        if not is_pos_int(self.seq):
            raise ValueError(f"seq must be a positive int, got {self.seq!r}")
        if not self.events:
            raise ValueError("a record needs at least one event")


def encode_record(record: LogRecord) -> bytes:
    """The record as compact UTF-8 JSON."""
    payload = {
        "seq": record.seq,
        "command": _command_to_json(record.command),
        "events": [_event_to_json(event) for event in record.events],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decode_record(data: bytes) -> LogRecord:
    """The record that `encode_record` wrote, or `LogFormatError`."""
    try:
        return _decode(data)
    except LogFormatError:
        raise
    except ValueError as error:
        # Not UTF-8, not JSON, an unknown enum value, or values that a domain
        # class refuses in its __post_init__.
        raise LogFormatError(f"invalid log record: {error}") from error


def _order_to_json(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "participant_id": order.participant_id,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "price": order.price,
        "time_in_force": order.time_in_force.value,
    }


def _command_to_json(command: Command) -> dict[str, Any]:
    match command:
        case Submit(order=order):
            return {"type": "Submit", "order": _order_to_json(order)}
        case Cancel(order_id=order_id):
            return {"type": "Cancel", "order_id": order_id}
        case Amend(order_id=order_id, price=price, quantity=quantity):
            return {
                "type": "Amend",
                "order_id": order_id,
                "price": price,
                "quantity": quantity,
            }
        case _:
            assert_never(command)


def _event_to_json(event: Event) -> dict[str, Any]:
    match event:
        case Accepted(seq=seq, order=order):
            return {"type": "Accepted", "seq": seq, "order": _order_to_json(order)}
        case Rejected(order_id=order_id, reason=reason):
            return {"type": "Rejected", "order_id": order_id, "reason": reason.value}
        case Trade():
            return {
                "type": "Trade",
                "trade_id": event.trade_id,
                "price": event.price,
                "quantity": event.quantity,
                "aggressor_order_id": event.aggressor_order_id,
                "resting_order_id": event.resting_order_id,
                "aggressor_side": event.aggressor_side.value,
                "aggressor_participant_id": event.aggressor_participant_id,
                "resting_participant_id": event.resting_participant_id,
            }
        case Cancelled(order_id=order_id, quantity=quantity, reason=reason):
            return {
                "type": "Cancelled",
                "order_id": order_id,
                "quantity": quantity,
                "reason": reason.value,
            }
        case Replaced():
            return {
                "type": "Replaced",
                "order_id": event.order_id,
                "seq": event.seq,
                "new_price": event.new_price,
                "new_total_quantity": event.new_total_quantity,
            }
        case _:
            assert_never(event)


def _decode(data: bytes) -> LogRecord:
    fields = _object(
        json.loads(data.decode("utf-8")), "record", {"seq", "command", "events"}
    )
    raw_events = fields["events"]
    if not isinstance(raw_events, list):
        raise LogFormatError("events must be a list")
    return LogRecord(
        seq=_int(fields, "seq"),
        command=_command_from_json(fields["command"]),
        events=tuple(_event_from_json(item) for item in cast(list[Any], raw_events)),
    )


def _command_from_json(value: object) -> Command:
    kind = _type_name(value, "command")
    match kind:
        case "Submit":
            fields = _object(value, "Submit", {"type", "order"})
            return Submit(order=_order_from_json(fields["order"]))
        case "Cancel":
            fields = _object(value, "Cancel", {"type", "order_id"})
            return Cancel(order_id=_str(fields, "order_id"))
        case "Amend":
            fields = _object(value, "Amend", {"type", "order_id", "price", "quantity"})
            return Amend(
                order_id=_str(fields, "order_id"),
                price=_int(fields, "price"),
                quantity=_int(fields, "quantity"),
            )
        case _:
            raise LogFormatError(f"unknown command type {kind!r}")


def _event_from_json(value: object) -> Event:
    kind = _type_name(value, "event")
    match kind:
        case "Accepted":
            fields = _object(value, "Accepted", {"type", "seq", "order"})
            return Accepted(
                seq=_int(fields, "seq"), order=_order_from_json(fields["order"])
            )
        case "Rejected":
            fields = _object(value, "Rejected", {"type", "order_id", "reason"})
            return Rejected(
                order_id=_str(fields, "order_id"),
                reason=RejectReason(_str(fields, "reason")),
            )
        case "Trade":
            fields = _object(
                value,
                "Trade",
                {
                    "type",
                    "trade_id",
                    "price",
                    "quantity",
                    "aggressor_order_id",
                    "resting_order_id",
                    "aggressor_side",
                    "aggressor_participant_id",
                    "resting_participant_id",
                },
            )
            return Trade(
                trade_id=_int(fields, "trade_id"),
                price=_int(fields, "price"),
                quantity=_int(fields, "quantity"),
                aggressor_order_id=_str(fields, "aggressor_order_id"),
                resting_order_id=_str(fields, "resting_order_id"),
                aggressor_side=Side(_str(fields, "aggressor_side")),
                aggressor_participant_id=_str(fields, "aggressor_participant_id"),
                resting_participant_id=_str(fields, "resting_participant_id"),
            )
        case "Cancelled":
            fields = _object(
                value, "Cancelled", {"type", "order_id", "quantity", "reason"}
            )
            return Cancelled(
                order_id=_str(fields, "order_id"),
                quantity=_int(fields, "quantity"),
                reason=CancelReason(_str(fields, "reason")),
            )
        case "Replaced":
            fields = _object(
                value,
                "Replaced",
                {"type", "order_id", "seq", "new_price", "new_total_quantity"},
            )
            return Replaced(
                order_id=_str(fields, "order_id"),
                seq=_int(fields, "seq"),
                new_price=_int(fields, "new_price"),
                new_total_quantity=_int(fields, "new_total_quantity"),
            )
        case _:
            raise LogFormatError(f"unknown event type {kind!r}")


def _order_from_json(value: object) -> Order:
    fields = _object(
        value,
        "order",
        {
            "order_id",
            "participant_id",
            "side",
            "order_type",
            "quantity",
            "price",
            "time_in_force",
        },
    )
    return Order(
        order_id=_str(fields, "order_id"),
        participant_id=_str(fields, "participant_id"),
        side=Side(_str(fields, "side")),
        order_type=OrderType(_str(fields, "order_type")),
        quantity=_int(fields, "quantity"),
        price=None if fields["price"] is None else _int(fields, "price"),
        time_in_force=TimeInForce(_str(fields, "time_in_force")),
    )


def _object(value: object, what: str, keys: set[str]) -> dict[str, Any]:
    """`value` as a JSON object with exactly the fields in `keys`."""
    if not isinstance(value, dict):
        raise LogFormatError(f"{what} must be an object")
    fields = cast(dict[str, Any], value)
    if set(fields) != keys:
        raise LogFormatError(
            f"{what} must have exactly the fields {sorted(keys)}, got {sorted(fields)}"
        )
    return fields


def _type_name(value: object, what: str) -> str:
    if not isinstance(value, dict):
        raise LogFormatError(f"{what} must be an object")
    kind = cast(dict[str, Any], value).get("type")
    if not isinstance(kind, str):
        raise LogFormatError(f"{what} needs a string type field")
    return kind


def _str(fields: dict[str, Any], key: str) -> str:
    value = fields[key]
    if not isinstance(value, str):
        raise LogFormatError(f"{key} must be a string, got {value!r}")
    return value


def _int(fields: dict[str, Any], key: str) -> int:
    value = fields[key]
    # bool is a subclass of int; reject it explicitly.
    if type(value) is not int:
        raise LogFormatError(f"{key} must be an integer, got {value!r}")
    return value
