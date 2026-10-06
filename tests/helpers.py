from orderbook_engine.engine.order_book import OrderBook


def assert_book_consistent(book: OrderBook) -> None:
    """The id index and the price levels agree, and the book is not crossed.

    Orders within a level are in strictly increasing seq order.

    Reads the book's private state: this is the one place tests look inside it.
    """
    bids = book._bids  # pyright: ignore[reportPrivateUsage]
    asks = book._asks  # pyright: ignore[reportPrivateUsage]
    index = book._orders  # pyright: ignore[reportPrivateUsage]

    ids_in_levels: list[str] = []
    for levels in (bids, asks):
        for level in levels.values():
            assert level, "an empty price level was left on the book"
            previous_seq = 0
            for order_id, resting in level.items():
                assert index.get(order_id) is resting, (
                    f"{order_id} is in a level but not in the id index"
                )
                assert resting.seq > previous_seq, (
                    f"{order_id} is not in increasing seq order within its level"
                )
                previous_seq = resting.seq
                ids_in_levels.append(order_id)

    assert len(ids_in_levels) == len(set(ids_in_levels)), (
        "an order id appears in more than one place in the levels"
    )
    assert set(index) == set(ids_in_levels), (
        "the id index and the levels hold different order ids"
    )

    if book.best_bid is not None and book.best_ask is not None:
        assert book.best_bid < book.best_ask, "the book is crossed"
