"""Properties of the sequencer under concurrent submitters, over random commands."""

import asyncio

from hypothesis import given
from strategies import command_logs

from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.app.sequencer import Outcome, Sequencer
from orderbook_engine.domain.commands import Command
from orderbook_engine.replay.checksum import state_checksum
from orderbook_engine.replay.replay import replay


async def submit_all(
    commands: list[Command], log: InMemoryLog
) -> tuple[list[Outcome], str]:
    """Submit every command at once. Returns the outcomes and the final checksum."""
    sequencer = Sequencer(log)
    await sequencer.start()
    try:
        outcomes = await asyncio.gather(*(sequencer.submit(c) for c in commands))
        return list(outcomes), state_checksum(sequencer.snapshot())
    finally:
        await sequencer.stop()


class TestSequencerProperties:
    # The generated logs reuse a few order ids, so some Submits repeat. The
    # sequencer answers an identical repeat from its table without a record.
    @given(commands=command_logs)
    def test_the_log_of_concurrent_submitters_replays_to_the_same_events_and_state(
        self, commands: list[Command]
    ) -> None:
        # given a random sequence of commands
        log = InMemoryLog()

        # when many clients submit them at the same time
        outcomes, checksum = asyncio.run(submit_all(commands, log))

        # then the log is contiguous from seq 1
        records = list(log.read_records())
        assert [record.seq for record in records] == list(range(1, len(records) + 1))

        # and each outcome equals the record that holds its seq
        assert outcomes == [
            Outcome(records[o.seq - 1].seq, records[o.seq - 1].events) for o in outcomes
        ]

        # and replaying the logged commands gives the logged events and the
        # sequencer's own state
        replayed = replay([record.command for record in records])
        assert replayed.events == [e for record in records for e in record.events]
        assert state_checksum(replayed.book.snapshot()) == checksum
