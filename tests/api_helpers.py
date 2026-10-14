"""Helpers for the API tests: start the app and talk to it in-process.

`httpx.ASGITransport` does not run the app's lifespan, so `serving` enters it
itself: that starts the sequencer on the way in and stops it on the way out.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orderbook_engine.adapters.api.app import create_app
from orderbook_engine.app.event_log import EventLog
from orderbook_engine.app.sequencer import Sequencer


@dataclass(frozen=True, slots=True)
class Served:
    """A running app: a client for it, and the sequencer it owns."""

    client: AsyncClient
    sequencer: Sequencer


@asynccontextmanager
async def serving_app(app: FastAPI) -> AsyncGenerator[Served]:
    """Run `app`'s lifespan and give a client for it."""
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield Served(client, app.state.sequencer)


@asynccontextmanager
async def serving(log: EventLog, *, queue_size: int = 1024) -> AsyncGenerator[Served]:
    """A new app on `log`, running."""
    async with serving_app(create_app(log, queue_size=queue_size)) as served:
        yield served


def order_body(order_id: str = "b1", **overrides: Any) -> dict[str, Any]:
    """The JSON body of a valid limit buy at 100. Overrides replace fields."""
    body: dict[str, Any] = {
        "order_id": order_id,
        "participant_id": "p1",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": 100,
    }
    return {**body, **overrides}
