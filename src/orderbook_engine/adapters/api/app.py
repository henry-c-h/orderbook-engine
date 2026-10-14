"""The FastAPI gateway: builds the app around one sequencer.

The app owns its sequencer. The lifespan starts it, which recovers the book
from the log, and stops it on shutdown. The gateway keeps no other state.

Run exactly one server worker. Each worker would build its own sequencer on the
same log, and two sequencers writing one log corrupt it.

Errors. An engine rejection is a normal outcome and returns 200. A full queue
returns 503 with `Retry-After`. A stopped or failed sequencer returns 503: the
client cannot know whether its command was logged. A Submit can be sent again
with the same ids and gets its original outcome if it was logged. A Cancel or
Amend cannot be retried that safely. Recovery needs a new process.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from orderbook_engine.adapters.api.routes import router
from orderbook_engine.app.event_log import EventLog
from orderbook_engine.app.sequencer import (
    OverloadedError,
    Sequencer,
    SequencerStoppedError,
)

RETRY_AFTER_SECONDS = 1


def create_app(
    log: EventLog,
    *,
    queue_size: int = 1024,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """An app that serves the book kept in `log`.

    `on_shutdown` runs after the sequencer has stopped, for example to close a
    log file.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        sequencer = Sequencer(log, queue_size=queue_size)
        try:
            await sequencer.start()
            app.state.sequencer = sequencer
            yield
        finally:
            await sequencer.stop()
            if on_shutdown is not None:
                on_shutdown()

    app = FastAPI(title="Order book", lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(OverloadedError, _overloaded)
    app.add_exception_handler(SequencerStoppedError, _stopped)
    return app


async def _overloaded(_request: Request, _error: Exception) -> JSONResponse:
    return JSONResponse(
        {"detail": "the command queue is full, retry shortly"},
        status_code=503,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


async def _stopped(_request: Request, _error: Exception) -> JSONResponse:
    return JSONResponse({"detail": "the sequencer is not running"}, status_code=503)
