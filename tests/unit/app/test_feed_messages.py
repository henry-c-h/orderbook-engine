import json

import pytest

from orderbook_engine.app.feed_messages import (
    FeedTrade,
    Heartbeat,
    LevelUpdate,
    PriceLevel,
    Snapshot,
    encode_message,
)
from orderbook_engine.domain.models import Side


class TestEncoding:
    def test_a_snapshot_lists_both_sides_as_price_and_quantity(self) -> None:
        # given a snapshot with two bid levels and one ask level
        snapshot = Snapshot(
            seq=42,
            bids=(PriceLevel(101, 8), PriceLevel(100, 12)),
            asks=(PriceLevel(103, 7),),
        )

        # when it is encoded
        text = encode_message(snapshot)

        # then it is the JSON of docs/FEED.md
        assert json.loads(text) == {
            "type": "snapshot",
            "seq": 42,
            "bids": [{"price": 101, "quantity": 8}, {"price": 100, "quantity": 12}],
            "asks": [{"price": 103, "quantity": 7}],
        }

    def test_a_level_update_carries_the_side_price_and_new_total(self) -> None:
        # given a level update
        update = LevelUpdate(seq=43, side=Side.BUY, price=101, quantity=5)

        # when it is encoded
        text = encode_message(update)

        # then the side is written as BUY or SELL
        assert json.loads(text) == {
            "type": "level",
            "seq": 43,
            "side": "BUY",
            "price": 101,
            "quantity": 5,
        }

    def test_a_trade_carries_no_order_or_participant_ids(self) -> None:
        # given a trade message
        trade = FeedTrade(
            seq=45, trade_id=17, price=103, quantity=7, aggressor_side=Side.SELL
        )

        # when it is encoded
        text = encode_message(trade)

        # then it has exactly the public fields
        assert json.loads(text) == {
            "type": "trade",
            "seq": 45,
            "trade_id": 17,
            "price": 103,
            "quantity": 7,
            "aggressor_side": "SELL",
        }

    def test_a_heartbeat_carries_only_the_last_seq(self) -> None:
        # given a heartbeat
        # when it is encoded
        text = encode_message(Heartbeat(seq=45))

        # then it holds its type and seq
        assert json.loads(text) == {"type": "heartbeat", "seq": 45}

    def test_the_text_is_compact_json(self) -> None:
        # given a level update
        # when it is encoded
        text = encode_message(LevelUpdate(seq=1, side=Side.SELL, price=100, quantity=0))

        # then it has no spaces between tokens
        assert " " not in text


class TestValidation:
    @pytest.mark.parametrize("seq", [0, -1])
    def test_a_level_update_needs_a_positive_seq(self, seq: int) -> None:
        # given a seq that no published message can have
        # when a level update is built
        # then it is refused
        with pytest.raises(ValueError):
            LevelUpdate(seq=seq, side=Side.BUY, price=100, quantity=1)

    def test_a_level_update_may_have_quantity_0_to_remove_the_level(self) -> None:
        # given quantity 0
        # when a level update is built
        update = LevelUpdate(seq=1, side=Side.BUY, price=100, quantity=0)

        # then it is accepted
        assert update.quantity == 0

    def test_a_level_update_refuses_a_negative_quantity_or_non_positive_price(
        self,
    ) -> None:
        # given invalid values
        # when level updates are built
        # then each is refused
        with pytest.raises(ValueError):
            LevelUpdate(seq=1, side=Side.BUY, price=100, quantity=-1)
        with pytest.raises(ValueError):
            LevelUpdate(seq=1, side=Side.BUY, price=0, quantity=1)

    def test_a_trade_needs_positive_values(self) -> None:
        # given a trade with quantity 0
        # when it is built
        # then it is refused
        with pytest.raises(ValueError):
            FeedTrade(seq=1, trade_id=1, price=100, quantity=0, aggressor_side=Side.BUY)

    def test_a_snapshot_and_a_heartbeat_may_have_seq_0_but_not_less(self) -> None:
        # given the seq of an empty feed
        # when they are built with 0 and with -1
        assert Snapshot(seq=0, bids=(), asks=()).seq == 0
        assert Heartbeat(seq=0).seq == 0

        # then -1 is refused
        with pytest.raises(ValueError):
            Snapshot(seq=-1, bids=(), asks=())
        with pytest.raises(ValueError):
            Heartbeat(seq=-1)
