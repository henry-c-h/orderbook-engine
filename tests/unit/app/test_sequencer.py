import asyncio
import random
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from builders import buy, sell
from strategies import records_for

from orderbook_engine.adapters.file_log import MAX_PAYLOAD_SIZE, FileLog
from orderbook_engine.app import sequencer as sequencer_module
from orderbook_engine.app.event_log import EventLog
from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.app.recovery import LogMismatchError
from orderbook_engine.app.sequencer import (
    Outcome,
    OverloadedError,
    Sequencer,
    SequencerStoppedError,
)
from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit
from orderbook_engine.domain.events import Event, Rejected, RejectReason
from orderbook_engine.engine.order_book import OrderBook
from orderbook_engine.replay.checksum import state_checksum
from orderbook_engine.replay.replay import replay


@asynccontextmanager
async def running(
    log: EventLog, *, queue_size: int = 1024
) -> AsyncGenerator[Sequencer]:
    """A started sequencer that is stopped when the block ends."""
    sequencer = Sequencer(log, queue_size=queue_size)
    await sequencer.start()
    try:
        yield sequencer
    finally:
        await sequencer.stop()


def outcome_of(record: LogRecord) -> Outcome:
    return Outcome(record.seq, record.events)


def checksum_of(sequencer: Sequencer) -> str:
    return state_checksum(sequencer.snapshot())


def resting_buy(
    order_id: str, *, price: int = 100, participant_id: str = "p1"
) -> Submit:
    return Submit(buy(order_id, price, participant_id=participant_id))


def random_commands(seed: int, count: int) -> list[Command]:
    """A seeded mix of submits, cancels and amends that meet each other."""
    rng = random.Random(seed)
    commands: list[Command] = []
    submitted: list[str] = []
    for number in range(count):
        draw = rng.random()
        if draw < 0.6 or not submitted:
            order_id = f"o{number}"
            submitted.append(order_id)
            side = rng.choice((buy, sell))
            order = side(
                order_id,
                rng.randint(95, 105),
                quantity=rng.randint(1, 10),
                participant_id=rng.choice(("p1", "p2", "p3")),
            )
            commands.append(Submit(order))
        elif draw < 0.8:
            commands.append(Cancel(rng.choice(submitted)))
        else:
            commands.append(
                Amend(rng.choice(submitted), rng.randint(95, 105), rng.randint(1, 10))
            )
    return commands


class TestSubmit:
    async def test_the_outcome_equals_the_record_in_the_log(self) -> None:
        # given a started sequencer on an empty log
        log = InMemoryLog()
        async with running(log) as sequencer:
            # when a command is submitted
            outcome = await sequencer.submit(resting_buy("b1"))

        # then the outcome carries seq 1 and the events of the one record
        [record] = log.read_records()
        assert outcome.seq == 1
        assert outcome == outcome_of(record)
        assert record.command == resting_buy("b1")

    async def test_concurrent_submitters_get_the_log_a_sequential_replay_gives(
        self,
    ) -> None:
        # given a seeded mix of commands and a started sequencer
        commands = random_commands(seed=7, count=300)
        log = InMemoryLog()
        async with running(log) as sequencer:
            # when many clients submit them at the same time
            outcomes = await asyncio.gather(
                *(sequencer.submit(command) for command in commands)
            )
            checksum = checksum_of(sequencer)

        # then the log is contiguous from seq 1 and holds every command
        records = list(log.read_records())
        assert [record.seq for record in records] == list(range(1, len(commands) + 1))
        assert {record.command for record in records} == set(commands)

        # and every outcome equals its record
        assert outcomes == [outcome_of(records[o.seq - 1]) for o in outcomes]

        # and replaying the logged commands gives the logged events and the
        # sequencer's own state
        replayed = replay([record.command for record in records])
        assert replayed.events == [e for record in records for e in record.events]
        assert state_checksum(replayed.book.snapshot()) == checksum

    async def test_a_command_is_still_logged_when_its_client_cancels(self) -> None:
        # given a started sequencer, and a client whose command is queued but
        # not yet taken by the worker (the worker only runs once the test awaits)
        log = InMemoryLog()
        async with running(log) as sequencer:
            client = asyncio.create_task(sequencer.submit(resting_buy("b1")))
            await asyncio.sleep(0)

            # when the client gives up while it waits
            client.cancel()
            with pytest.raises(asyncio.CancelledError):
                await client

            # and another client submits after it
            outcome = await sequencer.submit(resting_buy("b2"))

        # then the cancelled command was still processed and logged, and the
        # worker went on: the next command got the next seq
        records = list(log.read_records())
        assert [record.command for record in records] == [
            resting_buy("b1"),
            resting_buy("b2"),
        ]
        assert outcome == outcome_of(records[1])


class TestRestart:
    @staticmethod
    async def restart_and_compare(first_log: EventLog, second_log: EventLog) -> None:
        # given a sequencer that processed a mix of commands, then stopped
        commands = random_commands(seed=3, count=60)
        async with running(first_log) as first:
            outcomes = await asyncio.gather(*(first.submit(c) for c in commands))
            checksum = checksum_of(first)
        count = len(list(first_log.read_records()))
        earlier_submit = next(c for c in commands if isinstance(c, Submit))
        earlier_outcome = outcomes[commands.index(earlier_submit)]

        # when a new sequencer starts on the same log
        async with running(second_log) as second:
            # then it has the same book
            assert checksum_of(second) == checksum

            # and numbering continues after the last record
            new = await second.submit(
                resting_buy("fresh", price=90, participant_id="p9")
            )
            assert new.seq == count + 1

            # and resubmitting an earlier Submit returns its original outcome
            # and adds no record
            again = await second.submit(earlier_submit)
            assert again == earlier_outcome
        assert len(list(second_log.read_records())) == count + 1

    async def test_a_new_sequencer_on_the_same_in_memory_log_continues(self) -> None:
        # given one in-memory log
        log = InMemoryLog()

        # when a sequencer is restarted on it
        # then the book, numbering and idempotency carry over
        await self.restart_and_compare(log, log)

    async def test_a_new_sequencer_on_a_reopened_file_log_continues(
        self, tmp_path: Path
    ) -> None:
        # given a sequencer that processed a mix of commands on a file log, then
        # stopped, with the file closed
        path = tmp_path / "events.log"
        commands = random_commands(seed=11, count=40)
        with FileLog(path, fsync=False) as log:
            async with running(log) as sequencer:
                outcomes = await asyncio.gather(
                    *(sequencer.submit(c) for c in commands)
                )
                checksum = checksum_of(sequencer)

        # when the file is reopened and a new sequencer starts on it
        with FileLog(path, fsync=False) as reopened:
            count = len(list(reopened.read_records()))
            async with running(reopened) as restarted:
                # then the book, numbering and idempotency carry over
                assert checksum_of(restarted) == checksum
                new = await restarted.submit(
                    resting_buy("fresh", price=90, participant_id="p9")
                )
                assert new.seq == count + 1
                earlier = next(c for c in commands if isinstance(c, Submit))
                again = await restarted.submit(earlier)
                assert again == outcomes[commands.index(earlier)]
            assert len(list(reopened.read_records())) == count + 1

    async def test_a_log_that_does_not_match_the_engine_fails_start(self) -> None:
        # given a log whose second record holds the first record's events
        records = records_for(
            [resting_buy("b1"), Submit(sell("s1", 100, participant_id="p2"))]
        )
        tampered = [records[0], replace(records[1], events=records[0].events)]
        sequencer = Sequencer(InMemoryLog.from_records(tampered))

        # when it is started
        # then it raises
        with pytest.raises(LogMismatchError) as raised:
            await sequencer.start()
        assert raised.value.seq == 2


class TestIdempotency:
    async def test_the_same_submit_twice_at_once_is_logged_once(self) -> None:
        # given a started sequencer
        log = InMemoryLog()
        async with running(log) as sequencer:
            # when the same Submit arrives twice at the same time
            first, second = await asyncio.gather(
                sequencer.submit(resting_buy("b1")), sequencer.submit(resting_buy("b1"))
            )

            # then both get the same outcome, and the next command still gets seq 2
            assert first == second
            assert (await sequencer.submit(resting_buy("b2"))).seq == 2

        # and the log holds one record for it
        assert [r.command for r in log.read_records()] == [
            resting_buy("b1"),
            resting_buy("b2"),
        ]

    async def test_the_same_key_with_another_payload_is_processed_and_logged(
        self,
    ) -> None:
        # given a started sequencer that holds order b1 at price 100
        log = InMemoryLog()
        async with running(log) as sequencer:
            await sequencer.submit(resting_buy("b1", price=100))

            # when b1 arrives again at another price
            outcome = await sequencer.submit(resting_buy("b1", price=101))

        # then it is processed as a new command, and the engine rejects it
        assert outcome.seq == 2
        assert outcome.events == (Rejected("b1", RejectReason.DUPLICATE_ORDER_ID),)
        assert [r.seq for r in log.read_records()] == [1, 2]

    async def test_the_original_payload_still_gets_the_original_outcome(self) -> None:
        # given a log where b1 was submitted at 100, then again at 101
        log = InMemoryLog()
        async with running(log) as sequencer:
            original = await sequencer.submit(resting_buy("b1", price=100))
            await sequencer.submit(resting_buy("b1", price=101))

            # when the original is retried on the same sequencer
            live_retry = await sequencer.submit(resting_buy("b1", price=100))

        # and on a restarted one
        async with running(log) as restarted:
            restarted_retry = await restarted.submit(resting_buy("b1", price=100))

        # then both return the original outcome and add no record
        assert live_retry == original
        assert restarted_retry == original
        assert len(list(log.read_records())) == 2

    async def test_a_cancel_is_not_deduplicated(self) -> None:
        # given an order that was cancelled
        log = InMemoryLog()
        async with running(log) as sequencer:
            await sequencer.submit(resting_buy("b1"))
            first = await sequencer.submit(Cancel("b1"))

            # when the same Cancel is sent again
            second = await sequencer.submit(Cancel("b1"))

        # then it is processed again: it takes a seq and the engine rejects it
        assert second.seq == first.seq + 1
        assert second.events == (Rejected("b1", RejectReason.UNKNOWN_ORDER),)


class TestOverload:
    async def test_a_full_queue_rejects_the_rest_and_writes_nothing_for_them(
        self,
    ) -> None:
        # given a sequencer whose queue holds 2 commands
        log = InMemoryLog()
        async with running(log, queue_size=2) as sequencer:
            # when 10 commands are submitted at the same time
            results = await asyncio.gather(
                *(sequencer.submit(resting_buy(f"b{n}")) for n in range(10)),
                return_exceptions=True,
            )

        # then exactly 2 are accepted and the rest are overloaded
        accepted = [r for r in results if isinstance(r, Outcome)]
        overloaded = [r for r in results if isinstance(r, OverloadedError)]
        assert len(accepted) == 2
        assert len(overloaded) == 8

        # and only the accepted commands are in the log
        assert [r.command for r in log.read_records()] == [
            resting_buy("b0"),
            resting_buy("b1"),
        ]

    @pytest.mark.parametrize("queue_size", [0, -1])
    def test_a_queue_size_below_1_is_refused(self, queue_size: int) -> None:
        # given a queue size that asyncio would read as unbounded, or negative
        # when a sequencer is built with it
        # then it raises
        with pytest.raises(ValueError, match="queue_size"):
            Sequencer(InMemoryLog(), queue_size=queue_size)


class TestFailStop:
    @pytest.mark.parametrize("mode", ["before", "after"])
    async def test_the_failing_queued_and_later_commands_all_raise(
        self, mode: Literal["before", "after"]
    ) -> None:
        # given a log that fails at seq 2, and four commands submitted together
        log = InMemoryLog(fail_at_seq=2, fail_mode=mode)
        async with running(log) as sequencer:
            # when they are processed
            results = await asyncio.gather(
                *(sequencer.submit(resting_buy(f"b{n}")) for n in range(1, 5)),
                return_exceptions=True,
            )

            # then the first is acknowledged and the other three raise, with the
            # log's error as the cause
            assert isinstance(results[0], Outcome)
            for result in results[1:]:
                assert isinstance(result, SequencerStoppedError)
                assert isinstance(result.cause, OSError)

            # and so does every later submit
            with pytest.raises(SequencerStoppedError) as later:
                await sequencer.submit(resting_buy("b9"))
            assert isinstance(later.value.cause, OSError)

    @pytest.mark.parametrize("mode", ["before", "after"])
    async def test_a_new_sequencer_recovers_from_the_records_that_were_stored(
        self, mode: Literal["before", "after"]
    ) -> None:
        # given a sequencer that failed at seq 2 while processing b2
        failed_log = InMemoryLog(fail_at_seq=2, fail_mode=mode)
        async with running(failed_log) as sequencer:
            await sequencer.submit(resting_buy("b1"))
            with pytest.raises(SequencerStoppedError):
                await sequencer.submit(resting_buy("b2"))
        stored = list(failed_log.read_records())

        # when a new sequencer starts on a log rebuilt from the stored records
        async with running(InMemoryLog.from_records(stored)) as recovered:
            # then its book holds exactly the stored records
            expected = replay([record.command for record in stored])
            assert checksum_of(recovered) == state_checksum(expected.book.snapshot())

            # and resubmitting b2 is processed afresh if it was not stored, and
            # returns the original outcome, adding nothing, if it was
            again = await recovered.submit(resting_buy("b2"))
        if mode == "before":
            assert len(stored) == 1
            assert again.seq == 2
        else:
            assert len(stored) == 2
            assert again == outcome_of(stored[1])

    async def test_a_record_too_big_for_the_file_log_stops_the_sequencer(
        self, tmp_path: Path
    ) -> None:
        # given a file log, and an order whose id alone exceeds the maximum payload
        with FileLog(tmp_path / "events.log", fsync=False) as log:
            huge = resting_buy("x" * MAX_PAYLOAD_SIZE)
            async with running(log) as sequencer:
                await sequencer.submit(resting_buy("b1"))

                # when it and a command behind it are submitted together
                results = await asyncio.gather(
                    sequencer.submit(huge),
                    sequencer.submit(resting_buy("b3")),
                    return_exceptions=True,
                )

                # then both raise, with the log's ValueError as the cause
                for result in results:
                    assert isinstance(result, SequencerStoppedError)
                    assert isinstance(result.cause, ValueError)

                # and the snapshot is refused
                with pytest.raises(SequencerStoppedError):
                    sequencer.snapshot()

            # and the log holds only the first command, so a new sequencer works
            assert [r.seq for r in log.read_records()] == [1]
            async with running(log) as recovered:
                assert (await recovered.submit(resting_buy("b3"))).seq == 2

    async def test_an_engine_error_stops_the_sequencer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # given an engine that breaks on one command
        real_apply = sequencer_module.apply_command

        def buggy_apply(book: OrderBook, command: Command, seq: int) -> list[Event]:
            if command == Cancel("boom"):
                raise RuntimeError("engine bug")
            return real_apply(book, command, seq)

        monkeypatch.setattr(sequencer_module, "apply_command", buggy_apply)
        log = InMemoryLog()
        async with running(log) as sequencer:
            await sequencer.submit(resting_buy("b1"))

            # when that command and a later one are submitted together
            results = await asyncio.gather(
                sequencer.submit(Cancel("boom")),
                sequencer.submit(resting_buy("b2")),
                return_exceptions=True,
            )

            # then both raise, with the engine's error as the cause
            for result in results:
                assert isinstance(result, SequencerStoppedError)
                assert isinstance(result.cause, RuntimeError)

        # and the log holds only the first command
        assert [r.seq for r in log.read_records()] == [1]

    async def test_a_cancelled_worker_fails_every_waiting_command(self) -> None:
        # given a log whose append raises CancelledError at seq 2. This is
        # artificial: a real cancellation only lands while the worker waits on
        # the queue, because processing a command has no await. It checks that
        # the command in flight is failed too, not only the queued ones.
        class CancellingLog(InMemoryLog):
            def append(self, record: LogRecord) -> None:
                if record.seq == 2:
                    raise asyncio.CancelledError
                super().append(record)

        async with running(CancellingLog()) as sequencer:
            # when three commands are submitted together
            results = await asyncio.gather(
                *(sequencer.submit(resting_buy(f"b{n}")) for n in range(1, 4)),
                return_exceptions=True,
            )

            # then the first is acknowledged and none of the others hangs
            assert isinstance(results[0], Outcome)
            for result in results[1:]:
                assert isinstance(result, SequencerStoppedError)
                assert result.cause is None


class TestLifecycle:
    async def test_submit_before_start_raises(self) -> None:
        # given a sequencer that was not started
        sequencer = Sequencer(InMemoryLog())

        # when a command is submitted
        # then it raises
        with pytest.raises(SequencerStoppedError):
            await sequencer.submit(resting_buy("b1"))

    async def test_a_sequencer_runs_once(self) -> None:
        # given a started sequencer
        log = InMemoryLog()
        sequencer = Sequencer(log)
        await sequencer.start()

        # when it is started again, or started after it stopped
        # then it raises
        with pytest.raises(RuntimeError):
            await sequencer.start()
        await sequencer.stop()
        with pytest.raises(RuntimeError):
            await sequencer.start()

    async def test_stop_finishes_queued_commands_and_refuses_later_ones(
        self,
    ) -> None:
        # given a started sequencer with three commands queued
        log = InMemoryLog()
        sequencer = Sequencer(log)
        await sequencer.start()
        tasks = [
            asyncio.create_task(sequencer.submit(resting_buy(f"b{n}")))
            for n in range(1, 4)
        ]
        await asyncio.sleep(0)

        # when it is stopped
        await sequencer.stop()

        # then the queued commands finished
        assert [(await task).seq for task in tasks] == [1, 2, 3]
        assert len(list(log.read_records())) == 3

        # and later submits raise
        with pytest.raises(SequencerStoppedError) as raised:
            await sequencer.submit(resting_buy("b4"))
        assert raised.value.cause is None

    async def test_stop_can_be_called_twice_and_before_start(self) -> None:
        # given a sequencer that was stopped before it started, and one stopped twice
        never_started = Sequencer(InMemoryLog())
        started = Sequencer(InMemoryLog())
        await started.start()

        # when stop is called again
        await never_started.stop()
        await started.stop()
        await started.stop()

        # then nothing raises, and neither accepts commands
        for sequencer in (never_started, started):
            with pytest.raises(SequencerStoppedError):
                await sequencer.submit(resting_buy("b1"))

    async def test_stop_with_a_full_queue_finishes_every_queued_command(self) -> None:
        # given a sequencer whose queue is full: two commands queued, none taken
        sequencer = Sequencer(InMemoryLog(), queue_size=2)
        await sequencer.start()
        tasks = [
            asyncio.create_task(sequencer.submit(resting_buy(f"b{n}")))
            for n in range(1, 3)
        ]
        await asyncio.sleep(0)

        # when it is stopped
        async with asyncio.timeout(1):
            await sequencer.stop()

        # then both commands finished
        assert [(await task).seq for task in tasks] == [1, 2]

    async def test_a_cancelled_stop_can_be_repeated(self) -> None:
        # given a sequencer with a full queue, and a stop that has begun but has
        # not finished, because the worker has not yet taken the queued command
        sequencer = Sequencer(InMemoryLog(), queue_size=1)
        await sequencer.start()
        queued = asyncio.create_task(sequencer.submit(resting_buy("b1")))
        first_stop = asyncio.create_task(sequencer.stop())
        await asyncio.sleep(0)

        # when that stop is cancelled
        first_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_stop

        # then a second stop still completes, instead of waiting forever, and the
        # queued command finished
        async with asyncio.timeout(1):
            await sequencer.stop()
        assert (await queued).seq == 1
        with pytest.raises(SequencerStoppedError):
            await sequencer.submit(resting_buy("b2"))

    async def test_the_worker_sets_stopped_even_if_every_stop_was_cancelled(
        self,
    ) -> None:
        # given a stop that began and was cancelled while the worker still had
        # a queued command to finish
        sequencer = Sequencer(InMemoryLog(), queue_size=1)
        await sequencer.start()
        queued = asyncio.create_task(sequencer.submit(resting_buy("b1")))
        first_stop = asyncio.create_task(sequencer.stop())
        await asyncio.sleep(0)
        first_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_stop

        # when the worker finishes the queued command, with no stop() waiting
        await queued

        # then the sequencer is stopped, not left in the stopping state
        # (the state is not public, and nothing else shows the difference)
        assert sequencer._state.name == "STOPPED"  # pyright: ignore[reportPrivateUsage]


class TestSnapshot:
    async def test_it_is_refused_before_start(self) -> None:
        # given a sequencer that was not started
        sequencer = Sequencer(InMemoryLog())

        # when its snapshot is taken
        # then it raises
        with pytest.raises(SequencerStoppedError):
            sequencer.snapshot()

    async def test_it_works_while_running_and_after_a_graceful_stop(self) -> None:
        # given a sequencer that holds one resting order
        sequencer = Sequencer(InMemoryLog())
        await sequencer.start()
        await sequencer.submit(resting_buy("b1"))
        running_checksum = checksum_of(sequencer)

        # when it is stopped
        await sequencer.stop()

        # then the snapshot is still available and unchanged
        assert checksum_of(sequencer) == running_checksum
        assert sequencer.snapshot().bids[0].price == 100

    async def test_it_is_refused_after_a_failure(self) -> None:
        # given a sequencer whose log failed
        sequencer = Sequencer(InMemoryLog(fail_at_seq=1))
        await sequencer.start()
        with pytest.raises(SequencerStoppedError):
            await sequencer.submit(resting_buy("b1"))

        # when its snapshot is taken
        # then it raises, with the log's error as the cause
        with pytest.raises(SequencerStoppedError) as raised:
            sequencer.snapshot()
        assert isinstance(raised.value.cause, OSError)
        await sequencer.stop()
