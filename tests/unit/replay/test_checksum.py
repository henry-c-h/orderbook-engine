import re
from dataclasses import dataclass

import pytest
from builders import buy, sell

from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit
from orderbook_engine.engine.snapshot import (
    BookSnapshot,
    LevelSnapshot,
    OrderSnapshot,
)
from orderbook_engine.replay.checksum import encode_snapshot, state_checksum
from orderbook_engine.replay.replay import replay

EMPTY_SNAPSHOT = BookSnapshot(bids=(), asks=(), next_trade_id=1)
EMPTY_CHECKSUM = "0d966d842af2d55fa4cf7ef63f6cd34528044c4dd326920324f79582c90abb39"

# Written out by hand from the layout in the checksum module. The first id has
# a non-ASCII character, so its length prefix is 8 bytes for 7 characters.
GOLDEN_ENCODING = (
    "book-v1\n"
    "next_trade_id 2\n"
    "bids 2\n"
    "level 99 2\n"
    "order 8:ordér-1 2:p1 1 10 6\n"
    "order 2:b2 2:p2 3 5 5\n"
    "level 98 1\n"
    "order 2:b3 2:p1 4 7 7\n"
    "asks 1\n"
    "level 101 1\n"
    "order 2:s1 2:p3 2 8 8\n"
).encode()
GOLDEN_CHECKSUM = "9ad668ab3766ca6a7b789e037dfe4bd5316d674dabc17e88a439f5e1b79b03a5"


@dataclass(frozen=True, slots=True)
class Variant:
    """The golden book, with one field changed."""

    price: int = 99
    first_id: str = "ordér-1"
    first_participant: str = "p1"
    first_seq: int = 1
    first_total_quantity: int = 10
    first_remaining: int = 6
    next_trade_id: int = 2
    swap_first_two: bool = False
    ask_on_bid_side: bool = False


def golden_book(variant: Variant | None = None) -> BookSnapshot:
    variant = variant or Variant()
    first = OrderSnapshot(
        variant.first_id,
        variant.first_participant,
        variant.first_seq,
        variant.first_total_quantity,
        variant.first_remaining,
    )
    second = OrderSnapshot("b2", "p2", 3, 5, 5)
    level = (second, first) if variant.swap_first_two else (first, second)
    bids = (
        LevelSnapshot(variant.price, level),
        LevelSnapshot(98, (OrderSnapshot("b3", "p1", 4, 7, 7),)),
    )
    ask_level = LevelSnapshot(101, (OrderSnapshot("s1", "p3", 2, 8, 8),))
    if variant.ask_on_bid_side:
        return BookSnapshot(
            bids=(ask_level, *bids), asks=(), next_trade_id=variant.next_trade_id
        )
    return BookSnapshot(
        bids=bids, asks=(ask_level,), next_trade_id=variant.next_trade_id
    )


def one_order(order_id: str, participant_id: str, seq: int) -> LevelSnapshot:
    return LevelSnapshot(99, (OrderSnapshot(order_id, participant_id, seq, 10, 10),))


class TestEncodeSnapshot:
    def test_empty_book(self) -> None:
        # given an empty book's snapshot
        # when it is encoded
        encoded = encode_snapshot(EMPTY_SNAPSHOT)

        # then it is the version line, the trade counter and two empty sections
        assert encoded == b"book-v1\nnext_trade_id 1\nbids 0\nasks 0\n"

    def test_follows_the_documented_layout(self) -> None:
        # given a fixed book with a non-ASCII order id
        # when it is encoded
        encoded = encode_snapshot(golden_book())

        # then it matches the layout written out by hand
        assert encoded == GOLDEN_ENCODING

    def test_string_length_counts_bytes_not_characters(self) -> None:
        # given an order whose id is one character of two bytes
        snapshot = BookSnapshot(
            bids=(one_order("é", "p1", 1),), asks=(), next_trade_id=1
        )

        # when it is encoded
        encoded = encode_snapshot(snapshot)

        # then the id is written with a length of 2
        assert b"order 2:\xc3\xa9 2:p1 1 10 10\n" in encoded


class TestStateChecksum:
    def test_is_a_sha256_hex_digest(self) -> None:
        # given a book's snapshot
        # when its checksum is taken
        checksum = state_checksum(golden_book())

        # then it is 64 lowercase hex characters
        assert re.fullmatch(r"[0-9a-f]{64}", checksum)

    def test_empty_book_gives_a_stable_value(self) -> None:
        # given an empty book's snapshot
        # when its checksum is taken
        # then it is the pinned value
        assert state_checksum(EMPTY_SNAPSHOT) == EMPTY_CHECKSUM

    def test_fixed_small_book_gives_the_pinned_value(self) -> None:
        # given a fixed book with a non-ASCII order id
        # when its checksum is taken
        # then it is the pinned value
        assert state_checksum(golden_book()) == GOLDEN_CHECKSUM

    @pytest.mark.parametrize(
        "variant",
        [
            Variant(price=97),
            Variant(first_id="ordér-2"),
            Variant(first_participant="p9"),
            Variant(first_seq=5),
            Variant(first_total_quantity=11),
            Variant(first_remaining=5),
            Variant(next_trade_id=3),
            Variant(swap_first_two=True),
            Variant(ask_on_bid_side=True),
        ],
        ids=[
            "price",
            "order id",
            "participant",
            "seq",
            "total quantity",
            "remaining",
            "next trade id",
            "order position in a level",
            "side",
        ],
    )
    def test_changing_any_single_field_changes_the_checksum(
        self, variant: Variant
    ) -> None:
        # given the fixed book and a copy of it with one field changed
        original = golden_book()
        changed = golden_book(variant)

        # when their checksums are taken
        # then they differ
        assert state_checksum(changed) != state_checksum(original)

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(
                one_order("a b", "c", 1),
                one_order("a", "b c", 1),
                id="id and participant split at a space",
            ),
            pytest.param(
                one_order("a:b", "c", 1),
                one_order("a", "b:c", 1),
                id="id and participant split at a colon",
            ),
            pytest.param(
                one_order("1:a", "b", 1),
                one_order("1", "a:b", 1),
                id="id that looks like a length prefix",
            ),
            pytest.param(
                LevelSnapshot(
                    99,
                    (
                        OrderSnapshot("o1", "p1", 1, 10, 10),
                        OrderSnapshot("o2", "p1", 2, 10, 10),
                    ),
                ),
                one_order("o1 2:p1 1 10 10\norder 2:o2", "p1", 2),
                id="id that contains a second order line",
            ),
        ],
    )
    def test_books_that_a_naive_separator_would_encode_alike_differ(
        self, first: LevelSnapshot, second: LevelSnapshot
    ) -> None:
        # given two different books whose fields join into the same text
        # under a plain separator
        book_a = BookSnapshot(bids=(first,), asks=(), next_trade_id=1)
        book_b = BookSnapshot(bids=(second,), asks=(), next_trade_id=1)

        # when they are encoded and checksummed
        # then neither the encodings nor the checksums are equal
        assert encode_snapshot(book_a) != encode_snapshot(book_b)
        assert state_checksum(book_a) != state_checksum(book_b)


def checksum_after(commands: list[Command]) -> str:
    return state_checksum(replay(commands).book.snapshot())


class TestChecksumOfReplayedCommands:
    def test_replaying_the_same_commands_twice_gives_the_same_checksum(self) -> None:
        # given commands that rest, trade, amend and cancel
        commands: list[Command] = [
            Submit(buy("b1", 99)),
            Submit(buy("b2", 98, participant_id="p2")),
            Submit(sell("s1", 99, quantity=4, participant_id="p3")),
            Amend("b2", 99, 12),
            Cancel("b1"),
        ]

        # when they are replayed twice
        first = checksum_after(commands)
        second = checksum_after(commands)

        # then the checksums are equal
        assert first == second

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(
                [Submit(buy("b1", 99))],
                [Submit(buy("b1", 99)), Cancel("nope")],
                id="a rejected cancel after the same submit",
            ),
            pytest.param(
                [Submit(buy("b1", 99)), Submit(buy("b2", 98)), Cancel("b2")],
                [Submit(buy("b1", 99)), Cancel("nope"), Cancel("nope")],
                id="an order that came and went",
            ),
            pytest.param(
                [Submit(buy("b1", 99, quantity=7))],
                [Submit(buy("b1", 99)), Amend("b1", 99, 7)],
                id="an amend that keeps priority, against a submit of that total",
            ),
        ],
    )
    def test_equal_states_reached_by_different_histories_have_equal_checksums(
        self, first: list[Command], second: list[Command]
    ) -> None:
        # given two different sequences of commands that leave the same book:
        # the same resting orders with the same seq, and the same trade counter
        # when each is replayed and its book checksummed
        first_book = replay(first).book.snapshot()
        second_book = replay(second).book.snapshot()

        # then the snapshots are equal, and so are the checksums
        assert first_book == second_book
        assert state_checksum(first_book) == state_checksum(second_book)
