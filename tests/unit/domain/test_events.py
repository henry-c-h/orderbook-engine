import dataclasses
from typing import Any, get_args

import pytest

from orderbook_engine.domain.events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Rejected,
    RejectReason,
    Replaced,
)
from orderbook_engine.domain.models import Order, OrderType, Side, TimeInForce, Trade


def make_order() -> Order:
    return Order(
        order_id="o1",
        participant_id="p1",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=100,
        time_in_force=TimeInForce.GTC,
    )


def make_trade() -> Trade:
    return Trade(
        trade_id=1,
        price=100,
        quantity=10,
        aggressor_order_id="o2",
        resting_order_id="o1",
        aggressor_side=Side.SELL,
        aggressor_participant_id="p2",
        resting_participant_id="p1",
    )


def make_cancelled(*, quantity: Any = 10) -> Cancelled:
    return Cancelled(
        order_id="o1", quantity=quantity, reason=CancelReason.USER_REQUESTED
    )


def make_replaced(
    *, seq: Any = 2, new_price: Any = 101, new_total_quantity: Any = 5
) -> Replaced:
    return Replaced(
        order_id="o1",
        seq=seq,
        new_price=new_price,
        new_total_quantity=new_total_quantity,
    )


def all_events() -> list[Event]:
    return [
        Accepted(seq=1, order=make_order()),
        Rejected(order_id="o9", reason=RejectReason.UNKNOWN_ORDER),
        make_trade(),
        make_cancelled(),
        make_replaced(),
    ]


class TestEventUnion:
    def test_union_contains_exactly_the_five_spec_events(self) -> None:
        # `type Event = ...` makes a TypeAliasType; the union is in __value__.
        members = set(get_args(Event.__value__))
        assert members == {Accepted, Rejected, Trade, Cancelled, Replaced}


class TestEventsAreValueObjects:
    @pytest.mark.parametrize("event", all_events(), ids=lambda e: type(e).__name__)
    def test_is_immutable(self, event: Event) -> None:
        first_field = dataclasses.fields(event)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(event, first_field, None)

    def test_equal_fields_mean_equal_events(self) -> None:
        # Determinism checks compare event streams, so equality must be by value.
        assert make_cancelled() == make_cancelled()
        assert make_cancelled(quantity=10) != make_cancelled(quantity=9)


class TestAccepted:
    def test_carries_the_full_order_and_its_seq(self) -> None:
        order = make_order()
        accepted = Accepted(seq=3, order=order)
        assert accepted.seq == 3
        assert accepted.order is order

    @pytest.mark.parametrize("seq", [0, -1, True, 1.5, "1", None])
    def test_rejects_invalid_seq(self, seq: Any) -> None:
        with pytest.raises(ValueError, match="seq"):
            Accepted(seq=seq, order=make_order())


class TestRejected:
    def test_carries_order_id_and_reason(self) -> None:
        rejected = Rejected(order_id="o9", reason=RejectReason.UNKNOWN_ORDER)
        assert rejected.order_id == "o9"
        assert rejected.reason is RejectReason.UNKNOWN_ORDER


class TestCancelled:
    @pytest.mark.parametrize("reason", list(CancelReason))
    def test_accepts_every_reason(self, reason: CancelReason) -> None:
        cancelled = Cancelled(order_id="o1", quantity=4, reason=reason)
        assert cancelled.reason is reason
        assert cancelled.quantity == 4

    @pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "10", None])
    def test_rejects_invalid_quantity(self, quantity: Any) -> None:
        with pytest.raises(ValueError, match="quantity"):
            make_cancelled(quantity=quantity)


class TestReplaced:
    def test_carries_new_price_total_quantity_and_seq(self) -> None:
        replaced = make_replaced(new_price=101, new_total_quantity=5)
        assert (replaced.order_id, replaced.seq) == ("o1", 2)
        assert (replaced.new_price, replaced.new_total_quantity) == (101, 5)

    @pytest.mark.parametrize("seq", [0, -1, True, 1.5, "1", None])
    def test_rejects_invalid_seq(self, seq: Any) -> None:
        with pytest.raises(ValueError, match="seq"):
            make_replaced(seq=seq)

    @pytest.mark.parametrize("new_price", [0, -1, True, 100.5, "100", None])
    def test_rejects_invalid_new_price(self, new_price: Any) -> None:
        with pytest.raises(ValueError, match="new_price"):
            make_replaced(new_price=new_price)

    @pytest.mark.parametrize("new_total_quantity", [0, -1, True, 1.5, "10", None])
    def test_rejects_invalid_new_total_quantity(self, new_total_quantity: Any) -> None:
        with pytest.raises(ValueError, match="new_total_quantity"):
            make_replaced(new_total_quantity=new_total_quantity)
