"""The sequencer's hand-off to the market data feed."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from builders import buy
from feed_client import FeedClient
from strategies import records_for

from orderbook_engine.app.feed import Feed, SubscriberClosedError
from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.app.memory_log import InMemoryLog
from orderbook_engine.app.sequencer import Sequencer, SequencerStoppedError
from orderbook_engine.domain.commands import Cancel, Command, Submit
from orderbook_engine.domain.events import Cancelled, CancelReason


@asynccontextmanager
async def running(log: InMemoryLog, feed: Feed) -> AsyncGenerator[Sequencer]:
    sequencer = Sequencer(log, feed=feed)
    await sequencer.start()
    try:
        yield sequencer
    finally:
        await sequencer.stop()


class SpyFeed(Feed):
    """A feed that notes how many records the log held when each was published."""

    def __init__(self, log: InMemoryLog) -> None:
        super().__init__()
        self._log = log
        self.logged_at_publish: list[int] = []

    def publish(self, record: LogRecord) -> None:
        self.logged_at_publish.append(len(list(self._log.read_records())))
        super().publish(record)


class TestPublishing:
    async def test_a_logged_command_is_published(self) -> None:
        # given a running sequencer with a feed and a subscriber
        feed = Feed()
        subscriber = feed.subscribe()
        async with running(InMemoryLog(), feed) as sequencer:
            # when a buy is submitted
            await sequencer.submit(Submit(buy("b1", 100)))

            # then the subscriber receives the level update with feed seq 1
            assert json.loads(await subscriber.receive()) == {
                "type": "level",
                "seq": 1,
                "side": "BUY",
                "price": 100,
                "quantity": 10,
            }

    async def test_the_record_is_in_the_log_before_it_is_published(self) -> None:
        # given a feed that notes how many records the log holds at publish time
        log = InMemoryLog()
        feed = SpyFeed(log)
        async with running(log, feed) as sequencer:
            # when two commands are submitted
            await sequencer.submit(Submit(buy("b1", 100)))
            await sequencer.submit(Submit(buy("b2", 99)))

        # then each record was already appended when it was published
        assert feed.logged_at_publish == [1, 2]

    async def test_a_rejected_command_publishes_nothing_and_takes_no_number(
        self,
    ) -> None:
        # given a running sequencer with a subscriber
        feed = Feed()
        subscriber = feed.subscribe()
        async with running(InMemoryLog(), feed) as sequencer:
            # when a cancel of an unknown order, then a buy, are submitted
            await sequencer.submit(Cancel("nope"))
            await sequencer.submit(Submit(buy("b1", 100)))

            # then the first message the subscriber gets is number 1, although
            # the buy was the log's second record
            assert json.loads(await subscriber.receive())["seq"] == 1

    async def test_a_repeated_submit_is_published_once(self) -> None:
        # given a subscriber and a buy submitted twice with the same ids
        feed = Feed()
        subscriber = feed.subscribe()
        async with running(InMemoryLog(), feed) as sequencer:
            await sequencer.submit(Submit(buy("b1", 100)))
            await sequencer.submit(Submit(buy("b1", 100)))

            # then the feed holds one message, and the subscriber was sent only
            # that one
            assert feed.seq == 1
            assert json.loads(await subscriber.receive())["seq"] == 1
            assert subscriber.queued == 0


class TestRestart:
    async def test_a_new_sequencer_rebuilds_the_feed_from_the_log(self) -> None:
        # given a log written by a first sequencer and its feed
        log = InMemoryLog()
        first = Feed()
        async with running(log, first) as sequencer:
            for command in commands():
                await sequencer.submit(command)
            first_seq = first.seq
            first_snapshot = first.subscribe().snapshot

        # when a second sequencer starts on the same log with a new feed
        second = Feed()
        async with running(log, second) as sequencer:
            # then the new feed has the same numbers and the same snapshot
            assert second.seq == first_seq > 0
            subscriber = second.subscribe()
            assert subscriber.snapshot == first_snapshot

            # and it carries on from there
            await sequencer.submit(Submit(buy("b9", 90)))
            assert json.loads(await subscriber.receive())["seq"] == first_seq + 1

    async def test_the_numbers_do_not_depend_on_who_was_connected(self) -> None:
        # given the same commands run once with a subscriber and once without
        snapshots: list[str] = []
        for connected in (True, False):
            feed = Feed()
            if connected:
                feed.subscribe()
            async with running(InMemoryLog(), feed) as sequencer:
                for command in commands():
                    await sequencer.submit(command)
                snapshots.append(feed.subscribe().snapshot)

        # then both feeds ended in the same state
        assert snapshots[0] == snapshots[1]


class TestStopAndFailure:
    async def test_stopping_the_sequencer_closes_subscribers_with_1001(self) -> None:
        # given a running sequencer and a subscriber
        feed = Feed()
        subscriber = feed.subscribe()
        async with running(InMemoryLog(), feed) as sequencer:
            # when the sequencer is stopped
            await sequencer.stop()

        # then the subscriber is closed for shutdown
        with pytest.raises(SubscriberClosedError) as raised:
            await subscriber.receive()
        assert raised.value.code == 1001

    async def test_a_log_failure_closes_subscribers_with_1011(self) -> None:
        # given a sequencer whose log fails on the first append
        feed = Feed()
        subscriber = feed.subscribe()
        async with running(InMemoryLog(fail_at_seq=1), feed) as sequencer:
            # when a command is submitted
            with pytest.raises(SequencerStoppedError):
                await sequencer.submit(Submit(buy("b1", 100)))

        # then the subscriber is closed with the failure code, and was sent
        # nothing for the command that never reached the log
        with pytest.raises(SubscriberClosedError) as raised:
            await subscriber.receive()
        assert raised.value.code == 1011

    async def test_a_failed_feed_does_not_stop_the_sequencer(self) -> None:
        # given a feed that has failed
        feed = Feed()
        feed.publish(
            LogRecord(
                1,
                Cancel("ghost"),
                (Cancelled("ghost", 5, CancelReason.USER_REQUESTED),),
            )
        )
        assert feed.failed

        async with running(InMemoryLog(), feed) as sequencer:
            # when a command is submitted
            outcome = await sequencer.submit(Submit(buy("b1", 100)))

        # then the command is acknowledged as usual
        assert outcome.seq == 1


def commands() -> list[Command]:
    return [
        Submit(buy("b1", 100)),
        Submit(buy("b2", 99, quantity=4)),
        Cancel("nope"),
        Cancel("b1"),
    ]


def test_the_feed_a_client_builds_matches_the_records_of_the_log() -> None:
    # given the records of some commands, applied to a feed one by one
    feed = Feed()
    for record in records_for(commands()):
        feed.publish(record)

    # when a client reads the snapshot
    client = FeedClient()
    client.receive_text(feed.subscribe().snapshot)

    # then it holds the one level left, as of feed seq 3: two levels added,
    # nothing for the cancel of an unknown order, and one removal for b1
    assert client.book == ({99: 4}, {})
    assert client.seq == 3
