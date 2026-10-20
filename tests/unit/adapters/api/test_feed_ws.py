"""The WebSocket route of the market data feed.

These tests use Starlette's `TestClient`, which runs the app, its lifespan and
the sequencer on a background event loop. Calls that must run on that loop,
such as publishing to the feed directly, go through `on_loop`.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from api_helpers import order_body
from builders import buy
from feed_client import FeedClient, FeedGapError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from strategies import records_for

from orderbook_engine.adapters.api.app import create_app
from orderbook_engine.app.feed import Feed
from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.domain.commands import Submit


def on_loop[T](client: TestClient, call: Callable[[], T]) -> T:
    """Run `call` on the app's event loop, where the feed and sequencer live."""
    assert client.portal is not None
    return client.portal.call(call)


def read(websocket: Any) -> dict[str, Any]:
    return json.loads(websocket.receive_text())


def request(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """One HTTP call: the status code and the decoded JSON body."""
    response = client.request(method, path, json=body)
    return response.status_code, response.json()


def post(client: TestClient, order_id: str, **overrides: Any) -> None:
    status, _ = request(client, "POST", "/orders", order_body(order_id, **overrides))
    assert status == 200


def cancel(client: TestClient, order_id: str) -> None:
    request(client, "POST", "/orders/cancel", {"order_id": order_id})


class TestConnect:
    def test_the_first_message_is_a_snapshot_of_an_empty_book(self) -> None:
        # given a running app with no orders
        app = create_app(InMemoryLog())

        # when a client connects
        with TestClient(app) as client, client.websocket_connect("/feed") as websocket:
            first = read(websocket)

        # then it is told the book is empty, at seq 0
        assert first == {"type": "snapshot", "seq": 0, "bids": [], "asks": []}

    def test_the_snapshot_adds_up_the_orders_resting_at_each_price(self) -> None:
        # given two buys at 100 and one sell at 105
        with TestClient(create_app(InMemoryLog())) as client:
            post(client, "b1")
            post(client, "b2", quantity=5)
            post(client, "s1", side="SELL", price=105, participant_id="p2")

            # when a client connects
            with client.websocket_connect("/feed") as websocket:
                first = read(websocket)

        # then the snapshot has the totals, as of the third message
        assert first == {
            "type": "snapshot",
            "seq": 3,
            "bids": [{"price": 100, "quantity": 15}],
            "asks": [{"price": 105, "quantity": 10}],
        }


class TestUpdates:
    def test_a_new_order_is_followed_by_its_level_update(self) -> None:
        # given a connected client that has its snapshot
        app = create_app(InMemoryLog())
        with TestClient(app) as client, client.websocket_connect("/feed") as websocket:
            read(websocket)

            # when an order is placed
            post(client, "b1")

            # then the next message is the level update with seq 1
            assert read(websocket) == {
                "type": "level",
                "seq": 1,
                "side": "BUY",
                "price": 100,
                "quantity": 10,
            }

    def test_a_trade_is_followed_by_the_lowered_level(self) -> None:
        # given a resting buy and a connected client
        with TestClient(create_app(InMemoryLog())) as client:
            post(client, "b1")
            with client.websocket_connect("/feed") as websocket:
                read(websocket)

                # when a sell of 4 hits it
                post(client, "s1", side="SELL", quantity=4, participant_id="p2")

                # then the trade comes first, then the level with 6 left
                assert read(websocket) == {
                    "type": "trade",
                    "seq": 2,
                    "trade_id": 1,
                    "price": 100,
                    "quantity": 4,
                    "aggressor_side": "SELL",
                }
                assert read(websocket) == {
                    "type": "level",
                    "seq": 3,
                    "side": "BUY",
                    "price": 100,
                    "quantity": 6,
                }

    def test_a_rejected_command_publishes_nothing(self) -> None:
        # given a connected client
        app = create_app(InMemoryLog())
        with TestClient(app) as client, client.websocket_connect("/feed") as websocket:
            read(websocket)

            # when a cancel of an unknown order is followed by a real order
            cancel(client, "nope")
            post(client, "b1")

            # then the next message is number 1
            assert read(websocket)["seq"] == 1

    def test_no_order_id_or_participant_id_is_ever_sent(self) -> None:
        # given orders, a trade, a cancel and a rejection that use recognisable ids
        with TestClient(create_app(InMemoryLog())) as client:
            with client.websocket_connect("/feed") as websocket:
                snapshot_text = websocket.receive_text()
                post(client, "secret-order-1", participant_id="secret-party-1")
                post(
                    client,
                    "secret-order-2",
                    side="SELL",
                    quantity=3,
                    participant_id="secret-party-2",
                )
                cancel(client, "secret-order-1")
                cancel(client, "secret-order-1")
                post(client, "secret-order-3", participant_id="secret-party-3")
                # five messages follow: b1's level, the trade, the lowered
                # level, the cancel's removal and the third order's level
                texts = [snapshot_text] + [websocket.receive_text() for _ in range(5)]

            with client.websocket_connect("/feed") as later:
                texts.append(later.receive_text())

        # when everything the client received is searched
        # then no id appears in it
        assert not any("secret" in text for text in texts)


class TestHeartbeat:
    def test_an_idle_connection_gets_a_heartbeat_with_the_last_seq(self) -> None:
        # given an app that sends heartbeats every 50 ms, and one order placed
        app = create_app(InMemoryLog(), heartbeat_interval=0.05)
        with TestClient(app) as client:
            post(client, "b1")
            with client.websocket_connect("/feed") as websocket:
                read(websocket)

                # when the connection is idle
                message = read(websocket)

        # then the heartbeat carries seq 1 and uses no number
        assert message == {"type": "heartbeat", "seq": 1}

    def test_a_heartbeat_does_not_disturb_the_sequence(self) -> None:
        # given an idle connection that has received a heartbeat
        app = create_app(InMemoryLog(), heartbeat_interval=0.05)
        with TestClient(app) as client, client.websocket_connect("/feed") as websocket:
            feed_client = FeedClient()
            feed_client.receive(read(websocket))
            feed_client.receive(read(websocket))

            # when an order is placed, and the client reads until it has
            # the update (another heartbeat may arrive first)
            post(client, "b1")
            while feed_client.seq != 1:
                feed_client.receive(read(websocket))

        # then the client accepted the level update as number 1
        assert feed_client.seq == 1


class TestClosing:
    def test_a_closed_feed_closes_the_connection_with_its_code(self) -> None:
        # given a connected client
        app = create_app(InMemoryLog())
        with TestClient(app) as client, client.websocket_connect("/feed") as websocket:
            read(websocket)

            # when the feed is closed with 1011
            on_loop(client, lambda: app.state.feed.close(1011))

            # then the client sees that close code
            with pytest.raises(WebSocketDisconnect) as raised:
                websocket.receive_text()
            assert raised.value.code == 1011

    def test_a_connection_to_a_closed_feed_is_closed_with_the_feed_code(self) -> None:
        # given a feed that was closed for shutdown
        app = create_app(InMemoryLog())
        with TestClient(app) as client:
            on_loop(client, lambda: app.state.feed.close(1001))

            # when a client connects
            with client.websocket_connect("/feed") as websocket:
                # then it is closed at once with 1001
                with pytest.raises(WebSocketDisconnect) as raised:
                    websocket.receive_text()
                assert raised.value.code == 1001

    def test_the_connection_limit_closes_the_extra_client_with_1013(self) -> None:
        # given an app that allows one connection, already taken
        app = create_app(InMemoryLog(), feed_max_subscribers=1)
        with TestClient(app) as client, client.websocket_connect("/feed") as first:
            read(first)

            # when a second client connects
            with client.websocket_connect("/feed") as second:
                # then it is closed with 1013 and the first is unaffected
                with pytest.raises(WebSocketDisconnect) as raised:
                    second.receive_text()
                assert raised.value.code == 1013
            post(client, "b1")
            assert read(first)["seq"] == 1

    def test_a_connection_that_ends_frees_its_place(self) -> None:
        # given an app that allows one connection, used and closed
        app = create_app(InMemoryLog(), feed_max_subscribers=1)
        with TestClient(app) as client:
            with client.websocket_connect("/feed") as first:
                read(first)

            # when another client connects
            with client.websocket_connect("/feed") as second:
                # then it gets a snapshot
                assert read(second)["type"] == "snapshot"


class TestRecovery:
    def test_a_client_that_is_too_slow_is_closed_with_4001_and_resyncs(self) -> None:
        # given a connected client with room for 2 messages
        app = create_app(InMemoryLog(), feed_queue_size=2)
        with TestClient(app) as client:
            with client.websocket_connect("/feed") as websocket:
                feed_client = FeedClient()
                feed_client.receive(read(websocket))

                # when 5 messages are published in one turn of the event loop,
                # so the connection cannot send any of them in between
                records = records_for([Submit(buy(f"x{n}", 100 - n)) for n in range(5)])

                def publish_all() -> None:
                    for record in records:
                        app.state.feed.publish(record)

                on_loop(client, publish_all)

                # then the client is closed with 4001
                with pytest.raises(WebSocketDisconnect) as raised:
                    websocket.receive_text()
                assert raised.value.code == 4001

            # and when it reconnects, the snapshot holds all five levels
            feed_client.reconnect()
            with client.websocket_connect("/feed") as again:
                feed_client.receive(read(again))

        assert feed_client.seq == 5
        assert feed_client.book == ({100 - n: 10 for n in range(5)}, {})

    def test_a_client_that_misses_a_message_notices_and_recovers_by_snapshot(
        self,
    ) -> None:
        # given a connected client that is following the feed
        with TestClient(create_app(InMemoryLog())) as client:
            with client.websocket_connect("/feed") as websocket:
                feed_client = FeedClient()
                feed_client.receive(read(websocket))
                post(client, "b1")
                feed_client.receive(read(websocket))

                # when one message never reaches its book: three more orders
                # are placed and the second message is lost
                for order_id in ("b2", "b3", "b4"):
                    post(client, order_id, price=100 + int(order_id[1:]))
                lost = [read(websocket) for _ in range(3)]
                feed_client.receive(lost[0])
                with pytest.raises(FeedGapError):
                    feed_client.receive(lost[2])

            # then it reconnects, and the snapshot makes its book the server's
            feed_client.reconnect()
            with client.websocket_connect("/feed") as again:
                feed_client.receive(read(again))
            _, served = request(client, "GET", "/book")

        assert feed_client.book == (
            {level["price"]: level["quantity"] for level in served["bids"]},
            {level["price"]: level["quantity"] for level in served["asks"]},
        )
        assert feed_client.seq == 4


def test_the_app_keeps_its_feed_on_its_state() -> None:
    # given an app
    # when its lifespan has run
    app = create_app(InMemoryLog())
    with TestClient(app):
        # then it keeps its feed on its state
        assert isinstance(app.state.feed, Feed)
