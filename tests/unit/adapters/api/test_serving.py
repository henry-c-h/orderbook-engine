from dataclasses import replace
from pathlib import Path

import pytest
from api_helpers import order_body, serving_app
from builders import three_records

from orderbook_engine.adapters.api.app import create_app
from orderbook_engine.adapters.main import (
    LOG_PATH_VARIABLE,
    create_app_on_file,
    create_file_app,
)
from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.app.recovery import LogMismatchError


class TestRestart:
    async def test_a_new_app_on_the_same_file_shows_the_same_state(
        self, tmp_path: Path
    ) -> None:
        # given an app on a log file, with a resting order, a partly filled
        # order and a cancelled one
        path = tmp_path / "orders.log"
        async with serving_app(create_app_on_file(path, fsync=False)) as served:
            await served.client.post("/orders", json=order_body("b1"))
            await served.client.post("/orders", json=order_body("b2", price=99))
            await served.client.post(
                "/orders",
                json=order_body("s1", side="SELL", quantity=4, participant_id="p2"),
            )
            await served.client.post("/orders/cancel", json={"order_id": "b2"})
            book_before = (await served.client.get("/book")).json()
            order_before = (await served.client.get("/orders/b1")).json()

        # when the app is shut down and a new one is built on the same file
        async with serving_app(create_app_on_file(path, fsync=False)) as served:
            book_after = (await served.client.get("/book")).json()
            order_after = (await served.client.get("/orders/b1")).json()

            # and a client retries a Submit that was acknowledged before
            retry = await served.client.post("/orders", json=order_body("b1"))
            next_command = await served.client.post(
                "/orders", json=order_body("b3", price=98)
            )

        # then the book and the order are as they were
        assert book_before == {
            "bids": [{"price": 100, "quantity": 6, "orders": 1}],
            "asks": [],
        }
        assert book_after == book_before
        assert order_after == order_before
        assert order_after["remaining"] == 6

        # and the retry gets its original outcome, with seq 1
        assert retry.status_code == 200
        assert retry.json()["seq"] == 1
        assert [event["type"] for event in retry.json()["events"]] == ["Accepted"]

        # and numbering continues after the last logged command
        assert next_command.json()["seq"] == 5


class TestShutdown:
    async def test_on_shutdown_runs_after_the_app_stops(self) -> None:
        # given an app with an on_shutdown callback
        calls: list[str] = []
        app = create_app(InMemoryLog(), on_shutdown=lambda: calls.append("closed"))

        # when it runs and is shut down
        async with serving_app(app):
            assert calls == []

        # then the callback ran once, at the end
        assert calls == ["closed"]

    async def test_on_shutdown_runs_even_when_startup_fails(self) -> None:
        # given a log whose second record does not match what the engine produces
        first, second, _ = three_records()
        tampered = InMemoryLog.from_records(
            [first, replace(second, events=first.events)]
        )
        calls: list[str] = []
        app = create_app(tampered, on_shutdown=lambda: calls.append("closed"))

        # when the app is started
        # then startup fails with the recovery error, and the callback still ran
        with pytest.raises(LogMismatchError):
            async with serving_app(app):
                pytest.fail("the app must not start")
        assert calls == ["closed"]


class TestCompositionRoot:
    async def test_the_app_uses_the_log_file_named_by_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given a path in the environment variable
        path = tmp_path / "from-env.log"
        monkeypatch.setenv(LOG_PATH_VARIABLE, str(path))

        # when the app is built and an order is submitted
        async with serving_app(create_file_app()) as served:
            response = await served.client.post("/orders", json=order_body("b1"))

        # then the log file exists and holds the command
        assert response.status_code == 200
        assert path.exists()
        assert path.stat().st_size > 0
