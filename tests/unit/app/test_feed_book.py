import pytest
from builders import buy, market_order, sell
from strategies import records_for

from orderbook_engine.app.feed_book import FeedBook, FeedError
from orderbook_engine.app.feed_messages import (
    FeedTrade,
    LevelUpdate,
    PriceLevel,
    Snapshot,
)
from orderbook_engine.app.log_record import LogRecord
from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit
from orderbook_engine.domain.events import Accepted, Cancelled, CancelReason
from orderbook_engine.domain.models import Side, TimeInForce, Trade

type Message = LevelUpdate | FeedTrade


def published(commands: list[Command]) -> list[list[Message]]:
    """The messages each command publishes, one list per command."""
    book = FeedBook()
    return [list(book.apply(record)) for record in records_for(commands)]


def level(seq: int, side: Side, price: int, quantity: int) -> LevelUpdate:
    return LevelUpdate(seq=seq, side=side, price=price, quantity=quantity)


def trade(
    seq: int, trade_id: int, price: int, quantity: int, aggressor_side: Side
) -> FeedTrade:
    return FeedTrade(
        seq=seq,
        trade_id=trade_id,
        price=price,
        quantity=quantity,
        aggressor_side=aggressor_side,
    )


class TestResting:
    def test_an_empty_feed_has_seq_0_and_an_empty_snapshot(self) -> None:
        # given a feed that has seen no records
        book = FeedBook()

        # when it is read
        # then it is at seq 0 with no levels
        assert book.seq == 0
        assert book.snapshot() == Snapshot(seq=0, bids=(), asks=())

    def test_a_resting_order_publishes_its_level(self) -> None:
        # given a feed
        # when a buy of 10 rests at 100
        messages = published([Submit(buy("b1", 100))])

        # then one level update sets the total at 10, with seq 1
        assert messages == [[level(1, Side.BUY, 100, 10)]]

    def test_a_second_order_at_the_price_publishes_the_new_total(self) -> None:
        # given a buy of 10 resting at 100
        # when another buy of 5 rests at 100
        messages = published(
            [Submit(buy("b1", 100)), Submit(buy("b2", 100, quantity=5))]
        )

        # then the update carries the total, 15, and not the 5 that was added
        assert messages[1] == [level(2, Side.BUY, 100, 15)]

    def test_a_cancel_lowers_the_total_and_the_last_cancel_removes_the_level(
        self,
    ) -> None:
        # given two buys at 100, 10 and 5
        # when the first and then the second are cancelled
        messages = published(
            [
                Submit(buy("b1", 100)),
                Submit(buy("b2", 100, quantity=5)),
                Cancel("b1"),
                Cancel("b2"),
            ]
        )

        # then the total drops to 5 and then to 0
        assert messages[2] == [level(3, Side.BUY, 100, 5)]
        assert messages[3] == [level(4, Side.BUY, 100, 0)]


class TestTrading:
    def test_a_partial_fill_publishes_the_trade_then_the_lower_total(self) -> None:
        # given a buy of 10 resting at 100
        # when a sell of 4 hits it
        messages = published(
            [
                Submit(buy("b1", 100)),
                Submit(sell("s1", 100, quantity=4, participant_id="p2")),
            ]
        )

        # then the trade comes first, with the aggressor's side, then the
        # level with 6 left; the trade takes the next number
        assert messages[1] == [
            trade(2, 1, 100, 4, Side.SELL),
            level(3, Side.BUY, 100, 6),
        ]

    def test_a_full_fill_removes_the_level(self) -> None:
        # given a buy of 10 resting at 100
        # when a sell of 10 fills it
        messages = published(
            [Submit(buy("b1", 100)), Submit(sell("s1", 100, participant_id="p2"))]
        )

        # then the level is set to 0, and the seller's order never rests
        assert messages[1] == [
            trade(2, 1, 100, 10, Side.SELL),
            level(3, Side.BUY, 100, 0),
        ]

    def test_a_sweep_publishes_each_trade_in_match_order_then_the_levels(
        self,
    ) -> None:
        # given asks of 5 at 101 and 5 at 102
        # when a buy of 8 at 102 sweeps both
        messages = published(
            [
                Submit(sell("s1", 101, quantity=5, participant_id="p2")),
                Submit(sell("s2", 102, quantity=5, participant_id="p2")),
                Submit(buy("b1", 102, quantity=8)),
            ]
        )

        # then two trades at the resting prices come first, then the two
        # lowered levels, lowest price first; the buyer rests nothing
        assert messages[2] == [
            trade(3, 1, 101, 5, Side.BUY),
            trade(4, 2, 102, 3, Side.BUY),
            level(5, Side.SELL, 101, 0),
            level(6, Side.SELL, 102, 2),
        ]

    def test_a_remainder_that_rests_comes_after_the_decreases(self) -> None:
        # given an ask of 5 at 101
        # when a buy of 8 at 102 takes it and rests the other 3
        messages = published(
            [
                Submit(sell("s1", 101, quantity=5, participant_id="p2")),
                Submit(buy("b1", 102, quantity=8)),
            ]
        )

        # then the ask level is removed before the bid level appears, so the
        # client's book is never crossed between the two messages
        assert messages[1] == [
            trade(2, 1, 101, 5, Side.BUY),
            level(3, Side.SELL, 101, 0),
            level(4, Side.BUY, 102, 3),
        ]

    def test_an_id_can_be_used_again_once_its_order_has_filled(self) -> None:
        # given an order b1 that has been filled
        # when a new order with the id b1 rests at another price
        messages = published(
            [
                Submit(buy("b1", 100)),
                Submit(sell("s1", 100, participant_id="p2")),
                Submit(buy("b1", 99, quantity=5)),
            ]
        )

        # then it is a new level, and the filled order left nothing behind
        assert messages[2] == [level(4, Side.BUY, 99, 5)]


class TestNothingToPublish:
    @pytest.mark.parametrize(
        ("name", "commands"),
        [
            (
                "an IOC order that finds no liquidity",
                [Submit(buy("b1", 100, time_in_force=TimeInForce.IOC))],
            ),
            (
                "a market order that finds no liquidity",
                [Submit(market_order("m1", Side.BUY))],
            ),
            ("a cancel of an unknown order", [Cancel("nope")]),
            ("an amend of an unknown order", [Amend("nope", 100, 5)]),
            (
                "a submit with an id that is still live",
                [Submit(buy("b1", 100)), Submit(buy("b1", 99))],
            ),
            (
                "an amend below the filled quantity",
                [
                    Submit(buy("b1", 100)),
                    Submit(sell("s1", 100, quantity=4, participant_id="p2")),
                    Amend("b1", 100, 4),
                ],
            ),
            (
                "an order stopped by self-trade prevention before any fill",
                [
                    Submit(sell("s1", 101, participant_id="p1")),
                    Submit(buy("b1", 101, participant_id="p1")),
                ],
            ),
            (
                "an amend that leaves every level as it was",
                [Submit(buy("b1", 100)), Amend("b1", 100, 10)],
            ),
        ],
    )
    def test_a_record_with_no_public_change_publishes_nothing(
        self, name: str, commands: list[Command]
    ) -> None:
        # given a feed that has seen the earlier commands of a scenario
        book = FeedBook()
        records = records_for(commands)
        for record in records[:-1]:
            book.apply(record)
        seq_before = book.seq

        # when the last command of the scenario is applied (name: the scenario)
        messages = book.apply(records[-1])

        # then nothing is published and no number is used
        assert messages == (), name
        assert book.seq == seq_before

    def test_numbers_continue_without_a_hole_after_a_silent_record(self) -> None:
        # given a record that publishes nothing between two that do
        messages = published(
            [Submit(buy("b1", 100)), Cancel("nope"), Submit(buy("b2", 99))]
        )

        # then the numbers are 1 and 2
        assert messages == [
            [level(1, Side.BUY, 100, 10)],
            [],
            [level(2, Side.BUY, 99, 10)],
        ]


class TestSelfTradePrevention:
    def test_a_stopped_remainder_does_not_rest(self) -> None:
        # given asks of 3 from p2 and then 5 from p1, both at 101
        # when p1 buys 10 at 101: it fills p2's 3, then meets its own order
        messages = published(
            [
                Submit(sell("s1", 101, quantity=3, participant_id="p2")),
                Submit(sell("s2", 101, quantity=5, participant_id="p1")),
                Submit(buy("b1", 101, quantity=10, participant_id="p1")),
            ]
        )

        # then the fill is published and the level drops to 5, and no bid
        # level appears for the 7 that were cancelled
        assert messages[2] == [
            trade(3, 1, 101, 3, Side.BUY),
            level(4, Side.SELL, 101, 5),
        ]


class TestAmend:
    def test_lowering_the_quantity_in_place_publishes_the_lower_total(self) -> None:
        # given a buy of 10 at 100
        # when it is amended to 6 at the same price
        messages = published([Submit(buy("b1", 100)), Amend("b1", 100, 6)])

        # then the level total goes from 10 to 6
        assert messages[1] == [level(2, Side.BUY, 100, 6)]

    def test_a_new_price_removes_the_old_level_before_adding_the_new(self) -> None:
        # given a buy of 10 at 100
        # when it is amended to price 99
        messages = published([Submit(buy("b1", 100)), Amend("b1", 99, 10)])

        # then the decrease comes first
        assert messages[1] == [
            level(2, Side.BUY, 100, 0),
            level(3, Side.BUY, 99, 10),
        ]

    def test_a_level_touched_twice_is_published_once_with_its_final_total(
        self,
    ) -> None:
        # given two buys of 10 at 100
        # when the first is amended to 20 at the same price: it leaves the
        # level and arrives again
        messages = published(
            [
                Submit(buy("b1", 100)),
                Submit(buy("b2", 100)),
                Amend("b1", 100, 20),
            ]
        )

        # then one update carries the final total, 30
        assert messages[2] == [level(3, Side.BUY, 100, 30)]

    def test_an_amend_that_crosses_publishes_the_trade_and_both_removals(
        self,
    ) -> None:
        # given a buy of 10 at 100 and a sell of 10 at 105 from another participant
        # when the buy is amended to price 105
        messages = published(
            [
                Submit(buy("b1", 100)),
                Submit(sell("s1", 105, participant_id="p2")),
                Amend("b1", 105, 10),
            ]
        )

        # then the trade comes first, then both levels go to 0, bids first
        assert messages[2] == [
            trade(3, 1, 105, 10, Side.BUY),
            level(4, Side.BUY, 100, 0),
            level(5, Side.SELL, 105, 0),
        ]

    def test_a_lower_total_after_a_partial_fill_counts_what_was_filled(self) -> None:
        # given a buy of 10 at 100 that a sell of 4 has partly filled
        # when it is amended to a total of 8 at the same price
        messages = published(
            [
                Submit(buy("b1", 100)),
                Submit(sell("s1", 100, quantity=4, participant_id="p2")),
                Amend("b1", 100, 8),
            ]
        )

        # then 4 remain, because the 4 already filled still count in the total
        assert messages[2] == [level(4, Side.BUY, 100, 4)]

    def test_a_higher_total_after_a_partial_fill_rests_the_unfilled_part(self) -> None:
        # given the same partly filled buy
        # when it is amended to a total of 12 at the same price
        messages = published(
            [
                Submit(buy("b1", 100)),
                Submit(sell("s1", 100, quantity=4, participant_id="p2")),
                Amend("b1", 100, 12),
            ]
        )

        # then 8 remain
        assert messages[2] == [level(4, Side.BUY, 100, 8)]


class TestSnapshot:
    def test_levels_are_listed_best_price_first_with_totals(self) -> None:
        # given bids at 99, 101 (two orders) and 100, and asks at 104 and 103
        book = FeedBook()
        commands: list[Command] = [
            Submit(buy("b1", 99)),
            Submit(buy("b2", 101, quantity=4)),
            Submit(buy("b3", 101, quantity=3)),
            Submit(buy("b4", 100)),
            Submit(sell("s1", 104, quantity=2)),
            Submit(sell("s2", 103, quantity=6)),
        ]
        for record in records_for(commands):
            book.apply(record)

        # when the snapshot is taken
        snapshot = book.snapshot()

        # then bids run from the highest price down and asks from the lowest
        # up, with the total at each price, as of the last message
        assert snapshot == Snapshot(
            seq=6,
            bids=(PriceLevel(101, 7), PriceLevel(100, 10), PriceLevel(99, 10)),
            asks=(PriceLevel(103, 6), PriceLevel(104, 2)),
        )

    def test_a_snapshot_does_not_change_when_the_feed_moves_on(self) -> None:
        # given a snapshot taken after one order
        book = FeedBook()
        records = records_for([Submit(buy("b1", 100)), Submit(buy("b2", 99))])
        book.apply(records[0])
        snapshot = book.snapshot()

        # when another record is applied
        book.apply(records[1])

        # then the snapshot still shows the earlier state
        assert snapshot == Snapshot(seq=1, bids=(PriceLevel(100, 10),), asks=())


class TestInconsistentRecords:
    def test_a_cancel_of_an_order_the_feed_does_not_know_is_an_error(self) -> None:
        # given a record that cancels an order the feed never saw
        record = LogRecord(
            1,
            Cancel("ghost"),
            (Cancelled("ghost", 5, CancelReason.USER_REQUESTED),),
        )

        # when it is applied
        # then the feed refuses it
        with pytest.raises(FeedError):
            FeedBook().apply(record)

    def test_a_trade_against_an_unknown_resting_order_is_an_error(self) -> None:
        # given a record whose trade names a resting order the feed never saw
        order = sell("a", 100, quantity=5, participant_id="p2")
        record = LogRecord(
            1,
            Submit(order),
            (
                Accepted(1, order),
                Trade(
                    trade_id=1,
                    price=100,
                    quantity=5,
                    aggressor_order_id="a",
                    resting_order_id="ghost",
                    aggressor_side=Side.SELL,
                    aggressor_participant_id="p2",
                    resting_participant_id="p1",
                ),
            ),
        )

        # when it is applied
        # then the feed refuses it
        with pytest.raises(FeedError):
            FeedBook().apply(record)
