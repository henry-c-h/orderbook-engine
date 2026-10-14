import asyncio
import json
from typing import Any, get_args

import pytest
from api_helpers import order_body, serving
from builders import buy, sell, valid_trade

from orderbook_engine.adapters.api.routes import event_to_dict
from orderbook_engine.app.memory_log import InMemoryLog
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
from orderbook_engine.domain.models import Order
from orderbook_engine.replay.checksum import state_checksum
from orderbook_engine.replay.replay import replay

ACCEPTED_B1 = {
    "type": "Accepted",
    "seq": 1,
    "order": {
        "order_id": "b1",
        "participant_id": "p1",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": 100,
        "time_in_force": "GTC",
    },
}


class TestSubmit:
    async def test_an_order_is_accepted_and_logged(self) -> None:
        # given a running app on an empty log
        log = InMemoryLog()
        async with serving(log) as served:
            # when an order is submitted
            response = await served.client.post("/orders", json=order_body("b1"))

        # then the response carries its seq and events
        assert response.status_code == 200
        assert response.json() == {"seq": 1, "events": [ACCEPTED_B1]}

        # and the log holds that command with the same events
        [record] = log.read_records()
        assert record.command == Submit(buy("b1", 100))
        assert record.seq == 1

    async def test_an_order_that_trades_returns_the_trade_event(self) -> None:
        # given a resting buy
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))

            # when a sell crosses it
            response = await served.client.post(
                "/orders",
                json=order_body("s1", side="SELL", quantity=4, participant_id="p2"),
            )

        # then the events are the acceptance and the trade
        events = response.json()["events"]
        assert [event["type"] for event in events] == ["Accepted", "Trade"]
        assert events[1]["quantity"] == 4
        assert events[1]["price"] == 100
        assert events[1]["aggressor_order_id"] == "s1"
        assert events[1]["resting_order_id"] == "b1"

    async def test_a_repeated_identical_submit_returns_the_same_body_and_logs_once(
        self,
    ) -> None:
        # given an order that was submitted
        log = InMemoryLog()
        async with serving(log) as served:
            first = await served.client.post("/orders", json=order_body("b1"))

            # when the identical request is sent again
            second = await served.client.post("/orders", json=order_body("b1"))

        # then the response is the same
        assert second.status_code == 200
        assert second.json() == first.json()

        # and only one record was written
        assert len(list(log.read_records())) == 1


class TestCancelAndAmend:
    async def test_a_cancel_removes_the_order(self) -> None:
        # given a resting order
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))

            # when it is cancelled
            response = await served.client.post(
                "/orders/cancel", json={"order_id": "b1"}
            )

        # then the response carries seq 2 and the Cancelled event
        assert response.status_code == 200
        assert response.json() == {
            "seq": 2,
            "events": [
                {
                    "type": "Cancelled",
                    "order_id": "b1",
                    "quantity": 10,
                    "reason": "USER_REQUESTED",
                }
            ],
        }

    async def test_an_amend_changes_price_and_quantity(self) -> None:
        # given a resting order
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))

            # when it is amended
            response = await served.client.post(
                "/orders/amend", json={"order_id": "b1", "price": 99, "quantity": 5}
            )

        # then the response carries the Replaced event
        assert response.status_code == 200
        assert response.json() == {
            "seq": 2,
            "events": [
                {
                    "type": "Replaced",
                    "order_id": "b1",
                    "seq": 2,
                    "new_price": 99,
                    "new_total_quantity": 5,
                }
            ],
        }

    async def test_a_rejection_by_the_engine_is_a_200(self) -> None:
        # given an empty book
        log = InMemoryLog()
        async with serving(log) as served:
            # when an unknown order is cancelled
            response = await served.client.post(
                "/orders/cancel", json={"order_id": "nope"}
            )

        # then it is a normal outcome with a Rejected event, and it is logged
        assert response.status_code == 200
        assert response.json() == {
            "seq": 1,
            "events": [
                {
                    "type": "Rejected",
                    "order_id": "nope",
                    "reason": "UNKNOWN_ORDER",
                }
            ],
        }
        assert len(list(log.read_records())) == 1


class TestInvalidRequests:
    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/orders", order_body(quantity=True)),
            ("/orders", order_body(price=True)),
            ("/orders", order_body(quantity=1.0)),
            ("/orders", order_body(price=100.0)),
            ("/orders", order_body(quantity="10")),
            ("/orders", order_body(price="100")),
            ("/orders", order_body(quantity=10.5)),
            ("/orders", order_body(side="buy")),
            ("/orders", {**order_body(), "order_id": 5}),
            ("/orders", {**order_body(), "seq": 99}),
            ("/orders", {k: v for k, v in order_body().items() if k != "quantity"}),
            ("/orders/cancel", {"order_id": "b1", "participant_id": "p1"}),
            ("/orders/cancel", {"order_id": 5}),
            ("/orders/cancel", {}),
            ("/orders/amend", {"order_id": "b1", "price": True, "quantity": 5}),
            ("/orders/amend", {"order_id": "b1", "price": 5, "quantity": 1.0}),
            ("/orders/amend", {"order_id": "b1", "price": "5", "quantity": 5}),
            ("/orders/amend", {"order_id": "b1", "price": 5}),
        ],
    )
    async def test_a_body_of_the_wrong_shape_or_type_is_a_422(
        self, path: str, body: dict[str, Any]
    ) -> None:
        # given a running app
        log = InMemoryLog()
        async with serving(log) as served:
            # when a body with a wrong type, an extra field or a missing field is sent
            response = await served.client.post(path, json=body)

        # then it is refused and nothing is logged
        assert response.status_code == 422
        assert list(log.read_records()) == []

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/orders", order_body(quantity=0)),
            ("/orders", order_body(quantity=-1)),
            ("/orders", order_body(order_id="")),
            ("/orders", order_body(participant_id="")),
            ("/orders", order_body(price=None)),
            ("/orders", order_body(price=0)),
            ("/orders", order_body(order_type="MARKET", time_in_force="IOC")),
            ("/orders", order_body(order_type="MARKET", price=None)),
            ("/orders/cancel", {"order_id": ""}),
            ("/orders/amend", {"order_id": "", "price": 5, "quantity": 5}),
            ("/orders/amend", {"order_id": "b1", "price": 0, "quantity": 5}),
            ("/orders/amend", {"order_id": "b1", "price": 5, "quantity": -5}),
        ],
    )
    async def test_a_value_the_domain_refuses_is_a_422_not_a_500(
        self, path: str, body: dict[str, Any]
    ) -> None:
        # given a running app
        log = InMemoryLog()
        async with serving(log) as served:
            # when a body has the right types but a value the domain rejects
            response = await served.client.post(path, json=body)

        # then it is a 422, and nothing is logged
        assert response.status_code == 422
        assert list(log.read_records()) == []

    async def test_a_market_order_in_the_form_the_domain_accepts_is_served(
        self,
    ) -> None:
        # given a running app
        async with serving(InMemoryLog()) as served:
            # when a market IOC order without a price is submitted
            response = await served.client.post(
                "/orders",
                json=order_body(order_type="MARKET", time_in_force="IOC", price=None),
            )

        # then it is accepted for processing: with no liquidity it is cancelled
        assert response.status_code == 200
        types = [event["type"] for event in response.json()["events"]]
        assert types == ["Accepted", "Cancelled"]

    async def test_the_openapi_document_describes_the_request_models(self) -> None:
        # given a running app
        async with serving(InMemoryLog()) as served:
            # when its OpenAPI document is fetched
            response = await served.client.get("/openapi.json")

        # then the three request models are listed with their fields, and
        # unknown fields are marked as not allowed
        schemas = response.json()["components"]["schemas"]
        assert set(schemas) >= {"SubmitRequest", "CancelRequest", "AmendRequest"}
        assert set(schemas["SubmitRequest"]["properties"]) == {
            "order_id",
            "participant_id",
            "side",
            "order_type",
            "quantity",
            "price",
            "time_in_force",
        }
        assert set(schemas["CancelRequest"]["properties"]) == {"order_id"}
        assert set(schemas["AmendRequest"]["properties"]) == {
            "order_id",
            "price",
            "quantity",
        }
        for name in ("SubmitRequest", "CancelRequest", "AmendRequest"):
            assert schemas[name]["additionalProperties"] is False


class TestErrorMapping:
    async def test_a_full_queue_is_a_503_with_retry_after(self) -> None:
        # given an app whose queue holds one command
        log = InMemoryLog()
        async with serving(log, queue_size=1) as served:
            # when two requests arrive at the same time
            responses = await asyncio.gather(
                served.client.post("/orders", json=order_body("b1")),
                served.client.post("/orders", json=order_body("b2")),
            )

        # then one is accepted and the other is refused with Retry-After
        by_status = {response.status_code: response for response in responses}
        assert sorted(by_status) == [200, 503]
        assert by_status[503].headers["Retry-After"] == "1"

        # and only the accepted command was logged
        [record] = log.read_records()
        assert record.seq == by_status[200].json()["seq"]

    async def test_a_command_to_a_stopped_sequencer_is_a_503(self) -> None:
        # given an app whose sequencer has been stopped
        log = InMemoryLog()
        async with serving(log) as served:
            await served.sequencer.stop()

            # when a command arrives
            response = await served.client.post("/orders", json=order_body("b1"))

        # then it is a 503 without Retry-After, and nothing is logged
        assert response.status_code == 503
        assert "Retry-After" not in response.headers
        assert list(log.read_records()) == []

    async def test_a_log_failure_is_a_503_until_the_process_restarts(self) -> None:
        # given a log that fails on its second append
        log = InMemoryLog(fail_at_seq=2)
        async with serving(log) as served:
            first = await served.client.post("/orders", json=order_body("b1"))

            # when the failing command is sent, and then more requests
            failing = await served.client.post("/orders", json=order_body("b2"))
            later = await served.client.post("/orders", json=order_body("b3"))
            read = await served.client.get("/book")

        # then the first succeeded and everything after is a 503
        assert first.status_code == 200
        assert failing.status_code == 503
        assert later.status_code == 503
        assert read.status_code == 503

    async def test_a_retry_after_a_failure_gets_the_original_outcome_once_recovered(
        self,
    ) -> None:
        # given a command that was logged but whose acknowledgement failed
        log = InMemoryLog(fail_at_seq=1, fail_mode="after")
        async with serving(log) as served:
            failed = await served.client.post("/orders", json=order_body("b1"))
        assert failed.status_code == 503

        # when the client retries against a new app on the recovered log
        recovered = InMemoryLog.from_records(log.read_records())
        async with serving(recovered) as served:
            retry = await served.client.post("/orders", json=order_body("b1"))

        # then it gets the original outcome, and the log still has one record
        assert retry.status_code == 200
        assert retry.json() == {"seq": 1, "events": [ACCEPTED_B1]}
        assert len(list(recovered.read_records())) == 1


def _requests_for(commands: list[Command]) -> list[tuple[str, dict[str, Any]]]:
    """The HTTP path and body for each command."""
    requests: list[tuple[str, dict[str, Any]]] = []
    for command in commands:
        match command:
            case Submit(order=order):
                requests.append(("/orders", _body_of(order)))
            case Cancel(order_id=order_id):
                requests.append(("/orders/cancel", {"order_id": order_id}))
            case Amend(order_id=order_id, price=price, quantity=quantity):
                requests.append(
                    (
                        "/orders/amend",
                        {"order_id": order_id, "price": price, "quantity": quantity},
                    )
                )
    return requests


def _body_of(order: Order) -> dict[str, Any]:
    return order_body(
        order.order_id,
        participant_id=order.participant_id,
        side=order.side.value,
        quantity=order.quantity,
        price=order.price,
    )


def _mixed_commands(count: int) -> list[Command]:
    """Orders from three participants that cross, plus cancels and amends."""
    commands: list[Command] = []
    for number in range(count):
        side_buy = number % 2 == 0
        order_id = f"o{number}"
        price = 98 + (number * 7) % 5
        make = buy if side_buy else sell
        commands.append(Submit(make(order_id, price, participant_id=f"p{number % 3}")))
        if number % 5 == 4:
            commands.append(Cancel(f"o{number - 3}"))
        if number % 7 == 6:
            commands.append(Amend(f"o{number - 2}", price + 1, 3))
    return commands


class TestConcurrentClients:
    async def test_the_log_equals_a_sequential_replay_of_the_same_commands(
        self,
    ) -> None:
        # given a mix of crossing orders, cancels and amends
        commands = _mixed_commands(120)
        log = InMemoryLog()
        async with serving(log) as served:
            # when each is sent by its own client, all at the same time
            responses = await asyncio.gather(
                *(
                    served.client.post(path, json=body)
                    for path, body in _requests_for(commands)
                )
            )
            checksum = state_checksum(served.sequencer.snapshot())

        # then every request succeeded
        assert [response.status_code for response in responses] == [200] * len(commands)

        # and the log is contiguous and holds every command
        records = list(log.read_records())
        assert [record.seq for record in records] == list(range(1, len(commands) + 1))
        assert {record.command for record in records} == set(commands)

        # and replaying the logged commands in order gives the logged events
        # and the state the app reached
        replayed = replay([record.command for record in records])
        assert replayed.events == [e for record in records for e in record.events]
        assert state_checksum(replayed.book.snapshot()) == checksum

        # and each response carries the events of the record at its seq
        for response in responses:
            body = response.json()
            seq: int = body["seq"]
            assert body["events"] == [event_to_dict(e) for e in records[seq - 1].events]


class TestEventJson:
    def test_every_kind_of_event_is_tagged_with_its_class_name(self) -> None:
        # given one sample of each event class
        samples: list[Event] = [
            Accepted(seq=1, order=buy("b1", 100)),
            Rejected("b1", RejectReason.UNKNOWN_ORDER),
            valid_trade(),
            Cancelled("b1", 5, CancelReason.USER_REQUESTED),
            Replaced("b1", 2, 99, 5),
        ]

        # when they are converted
        converted = {type(event): event_to_dict(event) for event in samples}

        # then the samples cover every class in the Event union
        assert set(converted) == set(get_args(Event.__value__))

        # and each one is tagged with its class name and can be written as JSON
        for event_class, data in converted.items():
            assert data["type"] == event_class.__name__
            assert json.loads(json.dumps(data)) == data
