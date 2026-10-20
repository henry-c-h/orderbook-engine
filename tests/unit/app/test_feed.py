import asyncio
import inspect
import json
import logging
import random
from typing import Any

import pytest
from builders import buy, limit_order, sell
from feed_client import FeedClient
from strategies import records_for

from orderbook_engine.app.feed import (
    Feed,
    FeedUnavailableError,
    Subscriber,
    SubscriberClosedError,
)
from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.domain.commands import Cancel, Command, Submit
from orderbook_engine.domain.events import Cancelled, CancelReason
from orderbook_engine.domain.models import Side


async def drain(subscriber: Subscriber) -> list[dict[str, Any]]:
    """Every message waiting for the subscriber, decoded."""
    messages: list[dict[str, Any]] = []
    while True:
        try:
            text = await asyncio.wait_for(subscriber.receive(), timeout=0.01)
        except TimeoutError:
            return messages
        messages.append(json.loads(text))


def five_bids() -> list[LogRecord]:
    """Five records that each publish one message."""
    return records_for([Submit(buy(f"b{n}", 100 - n)) for n in range(5)])


def sweep_records() -> list[LogRecord]:
    """Three asks, then a buy that produces three trades and three removals."""
    commands: list[Command] = [
        Submit(sell(f"s{n}", 101 + n, quantity=5, participant_id="p2"))
        for n in range(3)
    ]
    commands.append(Submit(buy("b1", 103, quantity=15)))
    return records_for(commands)


def bad_record() -> LogRecord:
    """A record the feed cannot apply: it cancels an order it never saw."""
    return LogRecord(
        1, Cancel("ghost"), (Cancelled("ghost", 5, CancelReason.USER_REQUESTED),)
    )


class TestSubscribe:
    def test_a_new_subscriber_gets_a_snapshot_of_an_empty_feed(self) -> None:
        # given a feed that has published nothing
        feed = Feed()

        # when a client subscribes
        subscriber = feed.subscribe()

        # then its snapshot is at seq 0 with no levels
        assert json.loads(subscriber.snapshot) == {
            "type": "snapshot",
            "seq": 0,
            "bids": [],
            "asks": [],
        }
        assert feed.subscriber_count == 1

    def test_the_snapshot_holds_everything_published_before_it(self) -> None:
        # given a feed that has published two records
        feed = Feed()
        for record in five_bids()[:2]:
            feed.publish(record)

        # when a client subscribes
        subscriber = feed.subscribe()

        # then the snapshot is at seq 2 with both levels
        assert json.loads(subscriber.snapshot) == {
            "type": "snapshot",
            "seq": 2,
            "bids": [
                {"price": 100, "quantity": 10},
                {"price": 99, "quantity": 10},
            ],
            "asks": [],
        }

    async def test_messages_after_the_snapshot_start_at_the_next_seq(self) -> None:
        # given a subscriber that joined after one record
        feed = Feed()
        records = five_bids()
        feed.publish(records[0])
        subscriber = feed.subscribe()

        # when two more records are published
        feed.publish(records[1])
        feed.publish(records[2])

        # then it receives messages 2 and 3, in order
        messages = await drain(subscriber)
        assert [m["seq"] for m in messages] == [2, 3]
        assert [m["price"] for m in messages] == [99, 98]

    async def test_every_subscriber_gets_the_same_text(self) -> None:
        # given two subscribers
        feed = Feed()
        first = feed.subscribe()
        second = feed.subscribe()

        # when a record is published
        feed.publish(five_bids()[0])

        # then both receive identical text
        assert await first.receive() == await second.receive()

    def test_unsubscribing_frees_the_slot(self) -> None:
        # given a feed that allows one subscriber, taken by a client that left
        feed = Feed(max_subscribers=1)
        feed.unsubscribe(feed.subscribe())

        # when another client subscribes
        subscriber = feed.subscribe()

        # then it is accepted, and the feed counts only that one
        assert subscriber.close_code is None
        assert feed.subscriber_count == 1

    def test_the_subscriber_limit_refuses_the_next_connection_with_1013(self) -> None:
        # given a feed that allows two subscribers, both taken
        feed = Feed(max_subscribers=2)
        feed.subscribe()
        feed.subscribe()

        # when a third subscribes
        # then it is refused with code 1013 (try again later)
        with pytest.raises(FeedUnavailableError) as raised:
            feed.subscribe()
        assert raised.value.code == 1013


class TestSnapshotAtomicity:
    async def test_a_client_joining_between_publishes_never_sees_a_gap(self) -> None:
        # given a feed that publishes one record per turn of the event loop, and
        # clients that join at many different turns
        rng = random.Random(7)
        commands: list[Command] = [
            Submit(
                limit_order(
                    f"o{n}",
                    rng.choice(list(Side)),
                    rng.randint(95, 105),
                    quantity=rng.randint(1, 10),
                    participant_id=rng.choice(["p1", "p2", "p3"]),
                )
            )
            for n in range(60)
        ]
        feed = Feed(queue_size=1000)

        async def publisher() -> None:
            for record in records_for(commands):
                feed.publish(record)
                await asyncio.sleep(0)

        async def joiner(turns: int) -> Subscriber:
            for _ in range(turns):
                await asyncio.sleep(0)
            return feed.subscribe()

        # when they all run together: the joiners are scheduled first, and the
        # publisher runs in this task, handing control back after every record
        joining = asyncio.gather(*(joiner(turns) for turns in range(0, 60, 7)))
        await publisher()
        subscribers = await joining

        # then every client, applying its snapshot and then its messages,
        # reaches the feed's final state without a gap (the client raises on one)
        final = FeedClient()
        final.receive_text(feed.subscribe().snapshot)
        joined_at: set[int | None] = set()
        for subscriber in subscribers:
            client = FeedClient()
            client.receive_text(subscriber.snapshot)
            joined_at.add(client.seq)
            for message in await drain(subscriber):
                client.receive(message)
            assert client.seq == final.seq
            assert client.book == final.book
        assert len(joined_at) > 1, "the clients all joined at the same point"

    def test_publish_does_not_wait(self) -> None:
        # given a feed
        # when its publish method is inspected
        # then it is a plain function, so the sequencer never yields inside it
        assert not inspect.iscoroutinefunction(Feed.publish)
        assert not inspect.iscoroutinefunction(Feed.subscribe)


class TestSlowSubscriber:
    async def test_a_subscriber_whose_queue_fills_is_closed_with_4001(self) -> None:
        # given a subscriber with room for 3 messages that never reads
        feed = Feed(queue_size=3)
        subscriber = feed.subscribe()

        # when 5 messages are published
        for record in five_bids():
            feed.publish(record)

        # then it was dropped with code 4001 and is no longer a subscriber
        assert subscriber.close_code == 4001
        assert feed.subscriber_count == 0
        with pytest.raises(SubscriberClosedError) as raised:
            await subscriber.receive()
        assert raised.value.code == 4001

    async def test_a_slow_subscriber_does_not_affect_a_fast_one(self) -> None:
        # given one subscriber that never reads and one that reads each message
        feed = Feed(queue_size=3)
        slow = feed.subscribe()
        fast = feed.subscribe()

        # when 5 messages are published, with the fast one reading after each
        received: list[dict[str, Any]] = []
        for record in five_bids():
            feed.publish(record)
            received.extend(await drain(fast))

        # then the slow one is dropped and the fast one has all 5 in order
        assert slow.close_code == 4001
        assert fast.close_code is None
        assert [m["seq"] for m in received] == [1, 2, 3, 4, 5]

    async def test_one_records_messages_are_queued_together_or_not_at_all(
        self,
    ) -> None:
        # given a record that produces 6 messages and two feeds, with room
        # for 5 and for 6 messages
        *setup, sweep = sweep_records()
        small = Feed(queue_size=5)
        large = Feed(queue_size=6)
        for feed in (small, large):
            for record in setup:
                feed.publish(record)
        dropped = small.subscribe()
        kept = large.subscribe()

        # when the record is published to both
        small.publish(sweep)
        large.publish(sweep)

        # then the small queue drops its subscriber although the queue was
        # empty, and the large one receives all 6 messages
        assert dropped.close_code == 4001
        assert kept.close_code is None
        assert [m["seq"] for m in await drain(kept)] == [4, 5, 6, 7, 8, 9]


class TestClose:
    async def test_closing_tells_subscribers_the_code_and_refuses_new_ones(
        self,
    ) -> None:
        # given a feed with a subscriber
        feed = Feed()
        subscriber = feed.subscribe()

        # when the feed is closed for shutdown
        feed.close(1001)

        # then the subscriber is closed with 1001, and new ones are refused
        with pytest.raises(SubscriberClosedError) as raised:
            await subscriber.receive()
        assert raised.value.code == 1001
        assert feed.subscriber_count == 0
        with pytest.raises(FeedUnavailableError) as refused:
            feed.subscribe()
        assert refused.value.code == 1001

    def test_publishing_after_close_does_nothing(self) -> None:
        # given a closed feed
        feed = Feed()
        feed.close(1001)

        # when a record is published
        feed.publish(five_bids()[0])

        # then nothing is raised
        assert feed.seq == 0


class TestFailure:
    async def test_a_record_the_feed_cannot_apply_fails_the_feed_not_the_caller(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # given a feed with a subscriber
        feed = Feed()
        subscriber = feed.subscribe()

        # when it is given a record it cannot apply
        with caplog.at_level(logging.ERROR):
            feed.publish(bad_record())

        # then publish did not raise, the feed is failed and the failure is
        # logged, the subscriber is closed with 1011 and new ones are refused
        assert feed.failed
        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        with pytest.raises(SubscriberClosedError) as raised:
            await subscriber.receive()
        assert raised.value.code == 1011
        with pytest.raises(FeedUnavailableError) as refused:
            feed.subscribe()
        assert refused.value.code == 1011

    def test_a_failed_feed_ignores_later_records(self) -> None:
        # given a failed feed
        feed = Feed()
        feed.publish(bad_record())

        # when a valid record is published
        feed.publish(five_bids()[0])

        # then nothing is raised and the feed stays failed
        assert feed.failed


class TestHeartbeat:
    def test_a_heartbeat_carries_the_last_seq(self) -> None:
        # given a feed that has published two messages
        feed = Feed()
        for record in five_bids()[:2]:
            feed.publish(record)

        # when a heartbeat is made
        # then it carries seq 2
        assert json.loads(feed.heartbeat()) == {"type": "heartbeat", "seq": 2}

    def test_a_heartbeat_uses_no_number(self) -> None:
        # given a feed and a heartbeat made on it
        feed = Feed()
        feed.heartbeat()
        subscriber = feed.subscribe()

        # when the next record is published
        feed.publish(five_bids()[0])

        # then the feed is at seq 1, as if no heartbeat had been made
        assert feed.seq == 1
        assert json.loads(subscriber.snapshot)["seq"] == 0
