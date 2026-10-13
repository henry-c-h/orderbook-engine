"""The single writer in front of the engine.

Clients call `submit`, which puts the command on a bounded queue. One worker
task takes one command at a time, gives it the next seq, runs it through the
engine, appends one record (command, seq, events) to the event log, and only
then resolves the client's future. A command is acknowledged only after its
record is appended.

Durability. The append is synchronous and runs on the event loop, with no
await between the engine call and the append. Readers of `snapshot()` can
therefore never see a book that is ahead of the log. The cost: with `FileLog`
every command waits for an fsync with the whole loop blocked, so throughput is
one fsync per command, and `start()` blocks the loop while it reads the log.
Moving the append to a thread would let readers see state that is not yet
durable.

Idempotency. A Submit is identified by (participant_id, order_id). The first
Submit for a key is remembered with its Outcome. A later Submit with the same
key and an equal command returns that Outcome and writes nothing, and consumes
no seq. A later Submit with the same key and a different command is processed
and logged like any other, and never replaces the remembered one, so a retry of
the original still gets the original Outcome. The table is rebuilt from the log
at `start()`. It grows with every distinct Submit, with no eviction.
Cancel and Amend are not deduplicated: they carry no client request id, so a
retried Cancel consumes a seq and is answered by the engine, normally with an
unknown-order rejection.

Failure. If the engine or the log raises while a command is processed, the book
may be ahead of the log. The sequencer then fails: that command and every
queued command get `SequencerStoppedError` carrying the cause, later calls to
`submit` and `snapshot` raise it too, and the worker ends. The way back is a
new `Sequencer` on the same log (or on a log rebuilt from its records), which
recovers the book from the records. A client that gets `SequencerStoppedError`
for a command cannot know whether it was logged, and should resubmit it to the
new sequencer: a Submit gets its original Outcome if it was logged.

A client that cancels its `submit` call does not stop the command: it is still
processed and logged if it was queued.

Stopping. `stop()` closes the queue with `Queue.shutdown()`, so the worker
finishes the commands already queued and then ends. That method needs Python
3.13, which `requires-python` in pyproject.toml already demands.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum, auto

from orderbook_engine.app.event_log import EventLog
from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.app.recovery import recover_book
from orderbook_engine.domain.commands import Command, Submit
from orderbook_engine.domain.events import Event
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.engine.snapshot import BookSnapshot
from orderbook_engine.replay.replay import apply_command


class OverloadedError(Exception):
    """The queue is full. The command was not accepted and nothing was written."""


class SequencerStoppedError(Exception):
    """The sequencer is not running: not started, stopping, stopped or failed.

    `cause` is the exception that made the sequencer fail, if there was one.
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause
        self.__cause__ = cause


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a command did: the seq it was given and the events it produced."""

    seq: int
    events: tuple[Event, ...]


class _State(Enum):
    NEW = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()


type _Key = tuple[str, str]
type _Table = dict[_Key, tuple[Submit, Outcome]]
type _Item = tuple[Command, asyncio.Future[Outcome]]


class Sequencer:
    """Orders commands, runs them through the engine and logs them.

    Call `start()` before `submit()`, and `stop()` when done. One instance runs
    once: to run again, build a new one on the same log.
    """

    def __init__(self, log: EventLog, *, queue_size: int = 1024) -> None:
        if queue_size < 1:
            # asyncio treats a size of 0 as an unbounded queue.
            raise ValueError(f"queue_size must be at least 1, got {queue_size}")
        self._log = log
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=queue_size)
        self._book = OrderBook()
        self._next_seq = 1
        self._table: _Table = {}
        self._state = _State.NEW
        self._failure: Exception | None = None
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Rebuild the book and the idempotency table from the log, then run.

        Raises `LogMismatchError` if the log does not agree with the engine.
        Raises `RuntimeError` if the sequencer was started before.
        """
        if self._state is not _State.NEW:
            raise RuntimeError("the sequencer was already started")
        table: _Table = {}
        recovered = recover_book(_remembering_submits(self._log.read_records(), table))
        self._book = recovered.book
        self._next_seq = recovered.next_seq
        self._table = table
        self._state = _State.RUNNING
        self._worker = asyncio.create_task(self._run())

    async def submit(self, command: Command) -> Outcome:
        """Queue `command` and wait for its Outcome.

        Raises `OverloadedError` at once if the queue is full, and
        `SequencerStoppedError` if the sequencer is not running or fails while
        the command is queued or being processed.
        """
        if self._state is not _State.RUNNING:
            raise self._stopped()
        future: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
        try:
            self._queue.put_nowait((command, future))
        except asyncio.QueueFull:
            raise OverloadedError("the command queue is full") from None
        except asyncio.QueueShutDown:
            raise self._stopped() from None
        return await future

    async def stop(self) -> None:
        """Stop accepting commands, finish those already queued, end the worker.

        Safe to call again, also while an earlier call is still waiting: every
        call returns only once the worker has ended.
        """
        if self._worker is None:
            if self._state is _State.NEW:
                self._state = _State.STOPPED
            return
        if self._state is _State.RUNNING:
            self._state = _State.STOPPING
            # No await between the state change and the shutdown, so a
            # cancelled `stop()` cannot leave the queue open.
            self._queue.shutdown()
        # Not `await self._worker`: if the worker was cancelled, that would make
        # `stop()` raise `CancelledError`, as if `stop()` itself were cancelled.
        await asyncio.wait({self._worker})

    def snapshot(self) -> BookSnapshot:
        """The book as of the last logged command.

        Raises `SequencerStoppedError` before `start()` and after a failure,
        when the book may be ahead of the log.
        """
        if self._state in (_State.NEW, _State.FAILED):
            raise self._stopped()
        return self._book.snapshot()

    async def _run(self) -> None:
        in_flight: asyncio.Future[Outcome] | None = None
        finished = False
        try:
            while True:
                try:
                    command, in_flight = await self._queue.get()
                except asyncio.QueueShutDown:
                    # Raised only once the queue is shut down and empty. The
                    # worker sets the final state itself, so it is right even
                    # if every `stop()` call was cancelled while waiting.
                    finished = True
                    self._state = _State.STOPPED
                    return
                try:
                    outcome = self._process(command)
                except Exception as error:  # noqa: BLE001
                    # Whatever went wrong, the book may be ahead of the log.
                    self._state = _State.FAILED
                    self._failure = error
                    return
                # The client may have gone away: its future is then cancelled,
                # but the command was processed and logged all the same.
                if not in_flight.done():
                    in_flight.set_result(outcome)
                in_flight = None
        finally:
            if not finished:
                # Also when the worker is cancelled, so no client waits forever.
                self._state = _State.FAILED
                self._fail_pending(in_flight)

    def _process(self, command: Command) -> Outcome:
        """Run one command, append its record and return its Outcome."""
        if isinstance(command, Submit):
            stored = self._table.get(_key(command))
            if stored is not None and stored[0] == command:
                return stored[1]
        seq = self._next_seq
        events = tuple(apply_command(self._book, command, seq))
        self._log.append(LogRecord(seq, command, events))
        self._next_seq += 1
        outcome = Outcome(seq, events)
        _remember(self._table, command, outcome)
        return outcome

    def _fail_pending(self, in_flight: asyncio.Future[Outcome] | None) -> None:
        """Fail the command being processed and every queued command."""
        pending = [in_flight]
        while not self._queue.empty():
            pending.append(self._queue.get_nowait()[1])
        for future in pending:
            if future is not None and not future.done():
                future.set_exception(self._stopped())

    def _stopped(self) -> SequencerStoppedError:
        if self._state is _State.FAILED:
            return SequencerStoppedError(
                "the sequencer stopped after a failure", self._failure
            )
        if self._state is _State.NEW:
            return SequencerStoppedError("the sequencer was not started")
        return SequencerStoppedError("the sequencer is stopped")


def _key(submit: Submit) -> _Key:
    return (submit.order.participant_id, submit.order.order_id)


def _remember(table: _Table, command: Command, outcome: Outcome) -> None:
    """Record a Submit's Outcome under its key. The first one for a key stays."""
    if isinstance(command, Submit):
        table.setdefault(_key(command), (command, outcome))


def _remembering_submits(
    records: Iterable[LogRecord], table: _Table
) -> Iterator[LogRecord]:
    """Pass `records` through, filling `table` as `recover_book` checks them."""
    for record in records:
        yield record
        _remember(table, record.command, Outcome(record.seq, record.events))
