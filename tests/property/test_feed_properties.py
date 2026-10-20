"""Properties of the market data feed over random command logs.

Each log is run through the engine (by `play`, which also reads the book's own
state before and after every command) and through a `FeedBook`. The feed is
then compared with the book the engine really holds.
"""

import json
from dataclasses import dataclass

from feed_client import FeedClient, Levels, expected_levels
from hypothesis import event, given
from hypothesis import strategies as st
from strategies import command_logs, play

from orderbook_engine.app.feed_book import FeedBook
from orderbook_engine.app.feed_messages import (
    FeedTrade,
    LevelUpdate,
    Snapshot,
    encode_message,
)
from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.domain.commands import Command
from orderbook_engine.domain.events import Trade
from orderbook_engine.domain.models import Side
from orderbook_engine.engine.order_book import OrderBook

type Message = LevelUpdate | FeedTrade


@dataclass(frozen=True, slots=True)
class Stage:
    """One command: the book before and after it, and what the feed published."""

    record: LogRecord
    before: Levels
    after: Levels
    messages: tuple[Message, ...]
    snapshot: Snapshot


def stages(log: list[Command]) -> list[Stage]:
    feed = FeedBook()
    result: list[Stage] = []
    for step in play(OrderBook(), log):
        record = LogRecord(step.seq, step.command, tuple(step.events))
        messages = feed.apply(record)
        result.append(
            Stage(
                record=record,
                before=expected_levels(step.before),
                after=expected_levels(step.after),
                messages=tuple(messages),
                snapshot=feed.snapshot(),
            )
        )
        if not messages:
            event("a record that publishes nothing")
        elif any(isinstance(message, FeedTrade) for message in messages):
            event("a record that publishes a trade")
    return result


def client_from(snapshot: Snapshot) -> FeedClient:
    client = FeedClient()
    client.receive_text(encode_message(snapshot))
    return client


def all_messages(all_stages: list[Stage]) -> list[Message]:
    return [message for stage in all_stages for message in stage.messages]


class TestFeedProperties:
    @given(log=command_logs, data=st.data())
    def test_a_snapshot_plus_the_later_updates_equals_the_later_snapshot(
        self, log: list[Command], data: st.DataObject
    ) -> None:
        # given the snapshots after every command, the empty one first, and
        # every message the feed published
        all_stages = stages(log)
        snapshots = [
            Snapshot(seq=0, bids=(), asks=()),
            *(s.snapshot for s in all_stages),
        ]
        messages = all_messages(all_stages)

        # when a client starts from the snapshot after command i and applies the
        # messages numbered above it, up to the snapshot after command j
        i = data.draw(st.integers(0, len(snapshots) - 1), label="i")
        j = data.draw(st.integers(i, len(snapshots) - 1), label="j")
        client = client_from(snapshots[i])
        for message in messages:
            if snapshots[i].seq < message.seq <= snapshots[j].seq:
                client.receive_text(encode_message(message))

        # then its book and seq are those of the snapshot after command j
        later = client_from(snapshots[j])
        assert client.seq == later.seq
        assert client.book == later.book

    @given(log=command_logs)
    def test_every_snapshot_is_the_book_the_engine_holds(
        self, log: list[Command]
    ) -> None:
        # given a feed that has seen a random log
        all_stages = stages(log)

        for stage in all_stages:
            # when the snapshot after a command is read by a client
            snapshot = stage.snapshot
            client = client_from(snapshot)

            # then it equals the totals of the engine's resting orders, with
            # no empty level, bids falling and asks rising
            assert client.book == stage.after
            bid_prices = [level.price for level in snapshot.bids]
            ask_prices = [level.price for level in snapshot.asks]
            assert bid_prices == sorted(bid_prices, reverse=True)
            assert ask_prices == sorted(ask_prices)
            assert all(level.quantity > 0 for level in (*snapshot.bids, *snapshot.asks))

    @given(log=command_logs)
    def test_the_stream_is_gapless_and_the_book_is_never_crossed(
        self, log: list[Command]
    ) -> None:
        # given a client that connects to an empty feed
        all_stages = stages(log)
        client = client_from(Snapshot(seq=0, bids=(), asks=()))

        for stage in all_stages:
            # when it applies the messages of one command, one at a time
            for message in stage.messages:
                client.receive_text(encode_message(message))

                # then its book is not crossed at any message boundary (the
                # client raises if a number is skipped)
                assert not client.crossed

            # and after the command it holds the engine's book
            assert client.book == stage.after
            assert client.seq == stage.snapshot.seq

    @given(log=command_logs)
    def test_a_record_publishes_exactly_what_changed_in_a_fixed_order(
        self, log: list[Command]
    ) -> None:
        for stage in stages(log):
            # given the messages of one command
            trades = [m for m in stage.messages if isinstance(m, FeedTrade)]
            levels = [m for m in stage.messages if isinstance(m, LevelUpdate)]

            # then the trades are the engine's trades, in match order, and all
            # come before the level updates
            engine_trades = [e for e in stage.record.events if isinstance(e, Trade)]
            assert [
                (t.trade_id, t.price, t.quantity, t.aggressor_side) for t in trades
            ] == [
                (e.trade_id, e.price, e.quantity, e.aggressor_side)
                for e in engine_trades
            ]
            assert stage.messages == (*trades, *levels)

            # and the level updates are exactly the levels whose total changed,
            # each once, with the total after the command
            def total(book: Levels, side: Side, price: int) -> int:
                return (book[0] if side is Side.BUY else book[1]).get(price, 0)

            changed = {
                (side, price)
                for side, book_side in ((Side.BUY, 0), (Side.SELL, 1))
                for price in {*stage.before[book_side], *stage.after[book_side]}
                if total(stage.before, side, price) != total(stage.after, side, price)
            }
            assert len(levels) == len(changed)
            assert {(m.side, m.price) for m in levels} == changed
            for m in levels:
                assert m.quantity == total(stage.after, m.side, m.price)

            # and decreases come before increases, each group ordered by side
            # (BUY first) and then by ascending price
            decreases = [
                m for m in levels if m.quantity < total(stage.before, m.side, m.price)
            ]
            increases = [m for m in levels if m not in decreases]
            assert levels == decreases + increases
            for group in (decreases, increases):
                keys = [(m.side is Side.SELL, m.price) for m in group]
                assert keys == sorted(keys)

    @given(log=command_logs)
    def test_messages_hold_no_order_or_participant_data(
        self, log: list[Command]
    ) -> None:
        # given every message of a random log, and a snapshot after each command
        all_stages = stages(log)
        encoded = [
            *(encode_message(m) for m in all_messages(all_stages)),
            *(encode_message(s.snapshot) for s in all_stages),
        ]

        allowed_keys = {
            "snapshot": {"type", "seq", "bids", "asks"},
            "level": {"type", "seq", "side", "price", "quantity"},
            "trade": {"type", "seq", "trade_id", "price", "quantity", "aggressor_side"},
        }
        allowed_words = {"snapshot", "level", "trade", "BUY", "SELL"}

        for text in encoded:
            # when a message is read as JSON
            message = json.loads(text)

            # then it has only the public fields, and the only words in it
            # are message types and sides, so no order id or participant id
            # can be in it
            assert set(message) == allowed_keys[message["type"]]
            assert _words(message) <= allowed_words

    @given(log=command_logs, data=st.data())
    def test_a_feed_rebuilt_from_the_log_continues_with_the_same_numbers(
        self, log: list[Command], data: st.DataObject
    ) -> None:
        # given the records of a random log, and a restart after k of them
        records = [
            LogRecord(step.seq, step.command, tuple(step.events))
            for step in play(OrderBook(), log)
        ]
        k = data.draw(st.integers(0, len(records)), label="k")
        running = FeedBook()
        before_restart = [tuple(running.apply(record)) for record in records]

        # when a new feed replays the first k records, as the sequencer does at
        # start, and then takes the rest
        rebuilt = FeedBook()
        for record in records[:k]:
            rebuilt.apply(record)
        after_restart = [tuple(rebuilt.apply(record)) for record in records[k:]]

        # then it publishes the same messages as the feed that never stopped,
        # and ends in the same state
        assert after_restart == before_restart[k:]
        assert rebuilt.seq == running.seq
        assert rebuilt.snapshot() == running.snapshot()


def _words(value: object) -> set[str]:
    """Every string in a decoded JSON message, keys excluded."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(_words(item) for item in value.values()))  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    if isinstance(value, list):
        return set().union(*(_words(item) for item in value))  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return set()
