import dataclasses
from typing import Any, get_args

import pytest
from builders import valid_order

from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit


def all_commands() -> list[Command]:
    return [
        Submit(order=valid_order()),
        Cancel(order_id="o1"),
        Amend(order_id="o1", price=101, quantity=5),
    ]


class TestCommandUnion:
    def test_union_contains_exactly_the_three_commands(self) -> None:
        # given the command union
        # when its members are listed
        members = set(get_args(Command.__value__))

        # then they are submit, cancel and amend
        assert members == {Submit, Cancel, Amend}


class TestCommandsAreValueObjects:
    @pytest.mark.parametrize("command", all_commands(), ids=lambda c: type(c).__name__)
    def test_is_immutable(self, command: Command) -> None:
        # given a command
        first_field = dataclasses.fields(command)[0].name

        # when a field is assigned
        # then the command refuses
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(command, first_field, None)

    def test_equal_fields_mean_equal_commands(self) -> None:
        # given two amends built from the same values and one that differs
        first = Amend(order_id="o1", price=101, quantity=5)
        second = Amend(order_id="o1", price=101, quantity=5)
        other = Amend(order_id="o1", price=101, quantity=6)

        # when they are compared
        # then equality is by value
        assert first == second
        assert first != other


class TestSubmit:
    def test_carries_the_order(self) -> None:
        # given an order
        order = valid_order()

        # when it is wrapped in a submit
        command = Submit(order=order)

        # then the submit carries that order
        assert command.order is order


class TestCancel:
    def test_carries_the_order_id(self) -> None:
        # given an order id
        # when a cancel is built for it
        command = Cancel(order_id="o7")

        # then the cancel carries the id
        assert command.order_id == "o7"

    def test_rejects_empty_order_id(self) -> None:
        # given an empty order id
        # when a cancel is built for it
        # then it raises
        with pytest.raises(ValueError, match="order_id"):
            Cancel(order_id="")


class TestAmend:
    def test_carries_order_id_price_and_total_quantity(self) -> None:
        # given an order id, a new price and a new total quantity
        # when an amend is built from them
        command = Amend(order_id="o7", price=101, quantity=5)

        # then the amend carries all three
        assert (command.order_id, command.price, command.quantity) == ("o7", 101, 5)

    def test_rejects_empty_order_id(self) -> None:
        # given an empty order id
        # when an amend is built for it
        # then it raises
        with pytest.raises(ValueError, match="order_id"):
            Amend(order_id="", price=101, quantity=5)

    @pytest.mark.parametrize("price", [0, -1, True, 100.5, "100", None])
    def test_rejects_invalid_price(self, price: Any) -> None:
        # given an invalid price
        # when an amend is built with it
        # then it raises
        with pytest.raises(ValueError, match="price"):
            Amend(order_id="o1", price=price, quantity=5)

    @pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "10", None])
    def test_rejects_invalid_quantity(self, quantity: Any) -> None:
        # given an invalid quantity
        # when an amend is built with it
        # then it raises
        with pytest.raises(ValueError, match="quantity"):
            Amend(order_id="o1", price=101, quantity=quantity)
