"""The composition root: wires the file log, the sequencer and the API together.

Run it with

    ORDERBOOK_LOG_PATH=orderbook.log uv run python -m orderbook_engine.adapters.main

or, with your own uvicorn options, with

    uvicorn --factory orderbook_engine.adapters.main:create_file_app

Run exactly one server worker. Each worker builds its own sequencer on the
same log file, and two sequencers writing one log corrupt it. `main()` fixes
the worker count at one.
"""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from orderbook_engine.adapters.api.app import create_app
from orderbook_engine.adapters.file_log import FileLog

LOG_PATH_VARIABLE = "ORDERBOOK_LOG_PATH"
DEFAULT_LOG_PATH = "orderbook.log"


def create_app_on_file(
    path: Path, *, fsync: bool = True, queue_size: int = 1024
) -> FastAPI:
    """An app whose event log is the file at `path`, closed on shutdown.

    Opening the log repairs a torn tail. A corrupt log raises here, so the
    server does not start.
    """
    log = FileLog(path, fsync=fsync)
    return create_app(log, queue_size=queue_size, on_shutdown=log.close)


def create_file_app() -> FastAPI:
    """The app for the path in `ORDERBOOK_LOG_PATH` (default `orderbook.log`)."""
    return create_app_on_file(Path(os.environ.get(LOG_PATH_VARIABLE, DEFAULT_LOG_PATH)))


def main() -> None:
    uvicorn.run(create_file_app, factory=True, workers=1)


if __name__ == "__main__":
    main()
