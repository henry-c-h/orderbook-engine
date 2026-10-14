"""The endpoints. They hold no state: the sequencer is on `request.app.state`.

Every route is `async def`, so it runs on the event loop with the sequencer. A
plain `def` route would run in a thread pool and read the book from another
thread.
"""

from collections.abc import Sequence
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from orderbook_engine.adapters.api.models import (
    AmendRequest,
    CancelRequest,
    SubmitRequest,
)
from orderbook_engine.app.sequencer import Outcome, Sequencer
from orderbook_engine.domain.events import Event
from orderbook_engine.domain.models import Side
from orderbook_engine.engine.snapshot import LevelSnapshot

router = APIRouter()


def event_to_dict(event: Event) -> dict[str, Any]:
    """An event as JSON data. `type` names the event class, so a client can
    tell the kinds of event apart.
    """
    return {"type": type(event).__name__, **asdict(event)}


def _outcome_to_dict(outcome: Outcome) -> dict[str, Any]:
    return {"seq": outcome.seq, "events": [event_to_dict(e) for e in outcome.events]}


def _sequencer(request: Request) -> Sequencer:
    return request.app.state.sequencer


@router.post("/orders")
async def submit_order(body: SubmitRequest, request: Request) -> dict[str, Any]:
    """Place an order. A repeat of an identical request returns the same body."""
    return _outcome_to_dict(await _sequencer(request).submit(body.to_command()))


@router.post("/orders/cancel")
async def cancel_order(body: CancelRequest, request: Request) -> dict[str, Any]:
    return _outcome_to_dict(await _sequencer(request).submit(body.to_command()))


@router.post("/orders/amend")
async def amend_order(body: AmendRequest, request: Request) -> dict[str, Any]:
    return _outcome_to_dict(await _sequencer(request).submit(body.to_command()))


@router.get("/book")
async def get_book(
    request: Request, depth: Annotated[int | None, Query(ge=1)] = None
) -> dict[str, Any]:
    """The price levels of both sides, best first. `depth` limits each side."""
    snapshot = _sequencer(request).snapshot()
    return {
        "bids": _levels(snapshot.bids[:depth]),
        "asks": _levels(snapshot.asks[:depth]),
    }


@router.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request) -> dict[str, Any]:
    """An order that is resting on the book now.

    This is not a history lookup. The book keeps only resting orders, so an
    order that is unknown, fully filled or cancelled is a 404.
    """
    snapshot = _sequencer(request).snapshot()
    for side, levels in ((Side.BUY, snapshot.bids), (Side.SELL, snapshot.asks)):
        for level in levels:
            for order in level.orders:
                if order.order_id == order_id:
                    return {
                        "order_id": order.order_id,
                        "participant_id": order.participant_id,
                        "side": side.value,
                        "price": level.price,
                        "seq": order.seq,
                        "total_quantity": order.total_quantity,
                        "remaining": order.remaining,
                    }
    raise HTTPException(404, "not resting: unknown, filled or cancelled")


def _levels(levels: Sequence[LevelSnapshot]) -> list[dict[str, int]]:
    return [
        {
            "price": level.price,
            "quantity": sum(order.remaining for order in level.orders),
            "orders": len(level.orders),
        }
        for level in levels
    ]
