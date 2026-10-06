# Matching Engine Spec (v1)

Single instrument, single-threaded deterministic core. Everything else (async, API, UI) wraps this.

## Representation

- Prices are integer **ticks** (> 0). Quantities are integer **lots** (> 0). No floats anywhere.
- Ordering uses an engine-assigned monotonically increasing **sequence number** (integer, > 0), never wall-clock time.

## Order types

- `LIMIT` with `GTC` (rests on the book) or `IOC` (fill what you can now, cancel the rest).
- `MARKET` (always IOC, no price). Unfilled remainder is cancelled; no liquidity means zero fills and a cancel.
- Cancel by `order_id`. Unknown or already-filled/cancelled id -> `Rejected`.
- Amend = **cancel/replace**: same `order_id`, new price/qty. Loses time priority (new sequence number), except that lowering the quantity at the same price keeps it. See "Amend rules".

## Matching rules

1. Price priority: best price first (highest bid, lowest ask).
2. Time priority: FIFO within a price level, by sequence number.
3. A trade executes at the **resting order's price**.
4. Partial fills allowed. A GTC limit remainder rests; an IOC remainder is cancelled.
5. A limit order crosses if buy price >= best ask or sell price <= best bid.

## Amend rules

- An amend names a live order by `order_id` and gives a new price and a new **total** quantity. The total includes anything already filled, so the new remaining quantity is the new total minus the quantity already filled.
- The new total must be greater than the quantity already filled. Otherwise the amend is `Rejected(QUANTITY_BELOW_FILLED)` and the order is untouched.
- **Priority-preserving amend:** if the price is unchanged and the new total is lower than the current total, the order keeps its queue position and its sequence number. Its remaining quantity is reduced in place. No matching takes place, because the book cannot cross as a result.
- **Any other amend** (new price, or a total that is equal or higher) is processed as a newly arriving order: the order leaves its queue position, receives a new sequence number and is matched under the normal rules. If the new price crosses the book it trades immediately, and self-trade prevention applies. Any remainder rests as a GTC limit order.
- Events: `Replaced` first, then any `Trade` or `Cancelled` events produced by matching. `Replaced` carries the order's sequence number after the amend: the unchanged original for a priority-preserving amend, the new one otherwise. A priority-preserving amend emits only `Replaced`.
- Amending an unknown or already filled/cancelled id is `Rejected(UNKNOWN_ORDER)`.

## Order ids

- `order_id` is unique across all participants while the order is live. Submitting an order whose `order_id` is already live is `Rejected(DUPLICATE_ORDER_ID)` and the book is unchanged.
- Filled and cancelled orders are no longer live, and their ids may be reused. This keeps the engine's id index bounded by the number of live orders.
- The engine has no concept of client order ids. Mapping a participant's client order id to an engine `order_id` is the gateway's job.

## Self-trade prevention: cancel-newest

If the incoming order would match a resting order from the **same participant**, stop matching, cancel the incoming order's remaining quantity, and emit no trade for that pair. The resting order is untouched. Fills already executed against other participants earlier in the same sweep stand.

Self-trade prevention supports one mode (cancel newest, keyed by participant). Exchanges may also offer cancel-resting and cancel-both, selectable per order, which are out of scope.

## Events emitted (the engine's only output)

`Accepted`, `Rejected(reason)`, `Trade`, `Cancelled(reason)`, `Replaced`.

- Same input sequence must always produce the same event sequence (determinism is the basis for replay/backtest).
- Event order per command is Accepted/Replaced, then Trades in match order, then Cancelled. Rejected stands alone.

## Invariants (Hypothesis targets)

- Book is never crossed after any operation (best bid < best ask).
- Quantity conservation: for every order, current total quantity = filled + cancelled + resting. An amend changes the total.
- Within a level, resting orders are in strictly increasing sequence order.
- No trade has non-positive price or quantity; trade price equals the resting order's price.
- Replay of the same input log yields an identical event log.
- Sum of buyer-side fills equals sum of seller-side fills.

## Deliberately out of scope (state in README)

Multiple instruments, FOK, post-only, stop/iceberg orders, auctions, fees, authentication, multi-node replication, any claim about production latency.
