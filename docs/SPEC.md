# Matching Engine Spec (v1)

Single instrument, single-threaded deterministic core. Everything else (async, API, UI) wraps this.

## Representation

- Prices are integer **ticks** (> 0). Quantities are integer **lots** (> 0). No floats anywhere.
- Ordering uses an engine-assigned monotonically increasing **sequence number**, never wall-clock time.

## Order types

- `LIMIT` with `GTC` (rests on the book) or `IOC` (fill what you can now, cancel the rest).
- `MARKET` (always IOC, no price). Unfilled remainder is cancelled; no liquidity means zero fills and a cancel.
- Cancel by `order_id`. Unknown or already-filled/cancelled id -> `Rejected`.
- Amend = **cancel/replace**: same `order_id`, new price/qty, **new sequence number (loses time priority)**.

## Matching rules

1. Price priority: best price first (highest bid, lowest ask).
2. Time priority: FIFO within a price level, by sequence number.
3. A trade executes at the **resting order's price**.
4. Partial fills allowed. A GTC limit remainder rests; an IOC remainder is cancelled.
5. A limit order crosses if buy price >= best ask or sell price <= best bid.

## Self-trade prevention: cancel-newest

If the incoming order would match a resting order from the **same participant**, stop matching, cancel the incoming order's remaining quantity, and emit no trade for that pair. The resting order is untouched. Fills already executed against other participants earlier in the same sweep stand.

## Events emitted (the engine's only output)

`Accepted`, `Rejected(reason)`, `Trade`, `Cancelled(reason)`, `Replaced`.
Same input sequence must always produce the same event sequence (determinism is the basis for replay/backtest).

## Invariants (Hypothesis targets)

- Book is never crossed after any operation (best bid < best ask).
- Quantity conservation: submitted = filled + cancelled + resting.
- Within a level, resting orders are in strictly increasing sequence order.
- No trade has non-positive price or quantity; trade price equals the resting order's price.
- Replay of the same input log yields an identical event log.
- Sum of buyer-side fills equals sum of seller-side fills.

## Deliberately out of scope (state in README)

Multiple instruments, FOK, post-only, stop/iceberg orders, auctions, fees, authentication, multi-node replication, any claim about production latency.
