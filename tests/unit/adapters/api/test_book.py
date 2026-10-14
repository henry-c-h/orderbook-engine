from api_helpers import order_body, serving

from orderbook_engine.app.memory_log import InMemoryLog

NOT_RESTING = "not resting: unknown, filled or cancelled"


class TestBook:
    async def test_an_empty_book_has_no_levels(self) -> None:
        # given a running app with no orders
        async with serving(InMemoryLog()) as served:
            # when the book is read
            response = await served.client.get("/book")

        # then both sides are empty
        assert response.status_code == 200
        assert response.json() == {"bids": [], "asks": []}

    async def test_levels_are_summed_and_listed_best_price_first(self) -> None:
        # given bids at 100 (two orders) and 99, and asks at 102 and 103
        async with serving(InMemoryLog()) as served:
            for order_id, side, price, quantity in [
                ("b1", "BUY", 100, 10),
                ("b2", "BUY", 100, 5),
                ("b3", "BUY", 99, 7),
                ("s1", "SELL", 103, 4),
                ("s2", "SELL", 102, 6),
            ]:
                await served.client.post(
                    "/orders",
                    json=order_body(
                        order_id, side=side, price=price, quantity=quantity
                    ),
                )

            # when the book is read
            response = await served.client.get("/book")

        # then each side lists its levels with the total quantity and the
        # number of orders, best price first
        assert response.json() == {
            "bids": [
                {"price": 100, "quantity": 15, "orders": 2},
                {"price": 99, "quantity": 7, "orders": 1},
            ],
            "asks": [
                {"price": 102, "quantity": 6, "orders": 1},
                {"price": 103, "quantity": 4, "orders": 1},
            ],
        }

    async def test_a_partly_filled_order_counts_only_what_remains(self) -> None:
        # given a resting buy of 10 that a sell of 4 partly fills
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))
            await served.client.post(
                "/orders",
                json=order_body("s1", side="SELL", quantity=4, participant_id="p2"),
            )

            # when the book is read
            response = await served.client.get("/book")

        # then the level shows the 6 that remain
        assert response.json()["bids"] == [{"price": 100, "quantity": 6, "orders": 1}]

    async def test_depth_limits_the_levels_on_each_side(self) -> None:
        # given three bid levels and three ask levels
        async with serving(InMemoryLog()) as served:
            for number in range(3):
                await served.client.post(
                    "/orders", json=order_body(f"b{number}", price=100 - number)
                )
                await served.client.post(
                    "/orders",
                    json=order_body(f"s{number}", side="SELL", price=110 + number),
                )

            # when the book is read with a depth of 2
            response = await served.client.get("/book", params={"depth": 2})

        # then each side shows its two best levels
        body = response.json()
        assert [level["price"] for level in body["bids"]] == [100, 99]
        assert [level["price"] for level in body["asks"]] == [110, 111]

    async def test_a_depth_below_1_is_a_422(self) -> None:
        # given a running app
        async with serving(InMemoryLog()) as served:
            # when the book is read with depth 0
            response = await served.client.get("/book", params={"depth": 0})

        # then the request is refused
        assert response.status_code == 422

    async def test_the_book_is_a_503_after_a_log_failure(self) -> None:
        # given an app whose log failed on the first append
        async with serving(InMemoryLog(fail_at_seq=1)) as served:
            await served.client.post("/orders", json=order_body("b1"))

            # when the book is read
            response = await served.client.get("/book")

        # then the read is refused, because the book may be ahead of the log
        assert response.status_code == 503

    async def test_the_book_can_still_be_read_after_a_graceful_stop(self) -> None:
        # given an app whose sequencer was stopped normally
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))
            await served.sequencer.stop()

            # when the book is read
            response = await served.client.get("/book")

        # then it still shows the logged state
        assert response.status_code == 200
        assert response.json()["bids"] == [{"price": 100, "quantity": 10, "orders": 1}]


class TestOrderStatus:
    async def test_a_resting_order_is_returned_with_its_remaining_quantity(
        self,
    ) -> None:
        # given a resting buy of 10 that a sell of 4 partly fills
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))
            await served.client.post(
                "/orders",
                json=order_body("s1", side="SELL", quantity=4, participant_id="p2"),
            )

            # when the order is looked up
            response = await served.client.get("/orders/b1")

        # then its details show the 6 that remain
        assert response.status_code == 200
        assert response.json() == {
            "order_id": "b1",
            "participant_id": "p1",
            "side": "BUY",
            "price": 100,
            "seq": 1,
            "total_quantity": 10,
            "remaining": 6,
        }

    async def test_a_resting_sell_is_found_on_the_ask_side(self) -> None:
        # given a resting sell
        async with serving(InMemoryLog()) as served:
            await served.client.post(
                "/orders", json=order_body("s1", side="SELL", price=105)
            )

            # when it is looked up
            response = await served.client.get("/orders/s1")

        # then it is reported as a sell at its price
        assert response.json()["side"] == "SELL"
        assert response.json()["price"] == 105

    async def test_an_unknown_order_is_a_404(self) -> None:
        # given an empty book
        async with serving(InMemoryLog()) as served:
            # when an unknown order is looked up
            response = await served.client.get("/orders/nope")

        # then it is a 404 that says what that does and does not mean
        assert response.status_code == 404
        assert response.json() == {"detail": NOT_RESTING}

    async def test_a_filled_order_is_a_404(self) -> None:
        # given a buy that a sell of the same size fills completely
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))
            await served.client.post(
                "/orders", json=order_body("s1", side="SELL", participant_id="p2")
            )

            # when the filled order is looked up
            response = await served.client.get("/orders/b1")

        # then it is a 404
        assert response.status_code == 404
        assert response.json() == {"detail": NOT_RESTING}

    async def test_a_cancelled_order_is_a_404(self) -> None:
        # given a resting order that was cancelled
        async with serving(InMemoryLog()) as served:
            await served.client.post("/orders", json=order_body("b1"))
            await served.client.post("/orders/cancel", json={"order_id": "b1"})

            # when it is looked up
            response = await served.client.get("/orders/b1")

        # then it is a 404
        assert response.status_code == 404
        assert response.json() == {"detail": NOT_RESTING}
