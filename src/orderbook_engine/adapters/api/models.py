"""The HTTP request bodies, one per endpoint.

Each model checks the JSON and builds the domain command with `to_command()`.
Integers are strict, so `true`, `1.0` and `"100"` are refused instead of being
turned into numbers, and unknown fields are refused instead of ignored. The
domain classes still apply their own rules: a `ValueError` from one of them
becomes a 422 like any other invalid request.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from orderbook_engine.domain.commands import Amend, Cancel, Command, Submit
from orderbook_engine.domain.models import Order, OrderType, Side, TimeInForce


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_command(self) -> Command:
        raise NotImplementedError

    @model_validator(mode="after")
    def _the_domain_accepts_it(self) -> Self:
        # The domain classes raise ValueError for values they refuse, and
        # pydantic turns that into a 422.
        self.to_command()
        return self


class SubmitRequest(_Request):
    order_id: str
    participant_id: str
    side: Side
    order_type: OrderType
    quantity: StrictInt
    price: StrictInt | None = None
    time_in_force: TimeInForce = TimeInForce.GTC

    def to_command(self) -> Submit:
        return Submit(Order(**self.model_dump()))


class CancelRequest(_Request):
    order_id: str

    def to_command(self) -> Cancel:
        return Cancel(self.order_id)


class AmendRequest(_Request):
    order_id: str
    price: StrictInt
    quantity: StrictInt

    def to_command(self) -> Amend:
        return Amend(self.order_id, self.price, self.quantity)
