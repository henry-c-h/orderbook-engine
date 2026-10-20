# Market Data Feed (v1)

A public, read-only projection of the sequenced log, delivered over WebSocket.
It shows the total quantity at each price level and trades. Market data
vendors call this "Level 2" (depth by price), as opposed to "Level 3", which
lists individual orders; the terms are common usage, not a standard. It never
touches the matching path: the feed is a consumer that sees a record only after
the record has been appended to the log.

Rules of the matching engine live in [SPEC.md](SPEC.md).

## Endpoint

`WS /feed`. The client sends nothing; text or binary frames it sends are
ignored. The server's first message is always a `snapshot`.

## Representation

- Prices are integer ticks and quantities are integer lots, as in SPEC.md.
- Messages are JSON text frames, one message per frame.
- Sides use the domain's `BUY` and `SELL`. In a snapshot, `bids` are the BUY
  side and `asks` are the SELL side.
- There are no timestamps. The log holds none, and stamping a message when it
  is published would make the feed differ from one run to the next.

## Messages

```json
{"type":"snapshot","seq":42,"bids":[{"price":101,"quantity":8},{"price":100,"quantity":12}],"asks":[{"price":103,"quantity":7}]}
{"type":"level","seq":43,"side":"BUY","price":101,"quantity":5}
{"type":"level","seq":44,"side":"SELL","price":103,"quantity":0}
{"type":"trade","seq":45,"trade_id":17,"price":103,"quantity":7,"aggressor_side":"BUY"}
{"type":"heartbeat","seq":45}
```

| Type        | Fields                                          | Meaning                                                                                  |
| ----------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `snapshot`  | `seq`, `bids`, `asks`                           | The whole book as of `seq`. Each side lists its levels, best price first.                |
| `level`     | `seq`, `side`, `price`, `quantity`              | The new **total** quantity at that price. `0` removes the level.                         |
| `trade`     | `seq`, `trade_id`, `price`, `quantity`, `aggressor_side` | An execution, at the resting order's price. `trade_id` is the engine's trade counter. |
| `heartbeat` | `seq`                                           | The last sequence number assigned. Proves the connection is alive. Consumes no number.   |

A `level` message carries an absolute total, never a delta, so applying it
twice does the same as applying it once.

## Sequence numbers

The feed has its own sequence numbers, separate from the log's.

- Numbering starts at 1. Every `level` and every `trade` message takes the next
  number, with no gaps. A snapshot and a heartbeat take none.
- A `snapshot` with `seq = S` reflects exactly messages 1 to S. An empty feed
  has `S = 0`. The next message the client receives has `seq = S + 1`.
- A heartbeat carries the last number assigned. It is sent only when the
  connection has had no other message for one heartbeat interval (default
  one second), so a client may treat any message as a sign of life.
- The number is a function of the log and of the feed's code: the same log
  gives the same numbers. A client must not compare numbers across
  connections. The snapshot at the start of a connection replaces all state.

### Detecting a gap

A client keeps the `seq` of the last message it applied. It must discard its
book and reconnect when:

- a `level` or `trade` has `seq` other than `last + 1`, or
- a `heartbeat` has `seq` other than `last`, or
- the first message is not a `snapshot`.

Over TCP the server closes the connection instead of skipping a message, so a
gap should not occur on a healthy connection. The check is a safety net for
bugs, and the heartbeat's main use is liveness.

## What is public

Published:

- The total resting quantity at each price level.
- Each trade's `trade_id`, price, quantity and aggressor side.

Never published: order ids, participant ids, rejected commands, accepted
orders as such, cancel and replace events and their reasons, the log's
sequence numbers, and the number of orders at a level. Rejected commands
consume log sequence numbers, so showing those would reveal private activity.

Quantity changes are public by nature: a change at a level shows that an order
rested, was cancelled or traded there.

## What one log record publishes

The feed folds all the events of one log record, then publishes:

1. The `trade` messages, in match order.
2. One `level` message for each level whose total quantity differs from
   before the record. A level touched twice by one command appears once, with
   its final total.

Level messages go **decreases first, then increases**, each group ordered by
side (`BUY` first) and price (ascending). With this order the client's book is
never crossed between two messages. Reason: while the decreases are applied,
every level holds the smaller of its before and after quantity, so the book
holds only levels that exist both before and after, and both of those books
are uncrossed. During the increases it only holds levels of the after book.

A record that changes no public state publishes nothing and takes no number.
Examples: a rejected command, an IOC or market order that finds no liquidity,
an order cancelled by self-trade prevention before any fill, and an amend
that leaves every level total as it was. A repeated Submit that the sequencer
answers from its idempotency table writes no record, so it publishes nothing.

A command that produces many messages publishes them in one burst. A client
that is cut off in the middle of a burst reconnects, so it never works from a
half-applied command for long. There is no end-of-burst marker.

## Connecting

On connect the server accepts the WebSocket, takes the snapshot and registers
the connection in one step with no `await` in between, then sends the snapshot
and then every message with `seq > S` in order. The feed's publish step runs in
the same event loop task as the sequencer, so no message can be published
between the snapshot and the registration. The first message after the
snapshot is therefore always `S + 1`.

Taking the snapshot copies every level and blocks the event loop while it
runs. That is a known limitation for very large books and many simultaneous
connects.

## Slow clients

Each connection has a bounded queue of messages (1000 by default). One log
record's messages are queued together or not at all. If they do not fit, the
connection is dropped: its queue is discarded, the server sends a close frame
with code **4001** and the client reconnects and gets a new snapshot. Nothing
is conflated and nothing is retransmitted. A slow client never delays the
sequencer or another client.

The close frame goes behind the data already written to the client's socket,
so it reaches a client only if that client is still reading. A client that has
stopped reading altogether is never told: the server gives up on the
connection after its own timeouts, and the client sees the connection end
without a close frame. A client must therefore treat any end of the
connection as a reason to reconnect, whatever the code.

A single command that produces more messages than the queue holds drops every
connection. This is accepted and documented.

## Close codes

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| 1001 | The sequencer stopped while the server stayed up.                    |
| 1011 | The feed or the sequencer failed. Reconnect after the process is back. |
| 1013 | The feed is not ready, or the connection limit (100) is reached.     |
| 4001 | The client was too slow and was dropped.                             |

When the whole server process is stopped under uvicorn, uvicorn closes open
WebSockets itself, with 1012 (service restart), before the feed is closed. A
client therefore normally sees 1012, not 1001, on a planned shutdown, and a
connection reset after a crash.

A client should reconnect, with a delay that grows between attempts, after any
close or reset. The code says why the connection ended; it does not change what
the client does.

## Restart

The feed keeps no state of its own on disk. When the sequencer starts, it
rebuilds the feed from the log along with the book, so the numbering after a
restart continues where it was. Clients see the connection close (1012 on a
planned stop under uvicorn, a reset after a crash), reconnect and receive a
snapshot. That snapshot may be ahead of what the client last saw, because the
log holds everything published and possibly more. It is never behind unless an
operator restores an older log, which is why clients do not compare numbers
across connections.

If the feed itself fails (for example it meets an event it cannot apply), it
closes its connections with 1011 and refuses new ones. The sequencer keeps
running and acknowledging commands, and the failure is logged.

## Consistency with the book

The feed keeps its own book, built only from the events in the log records. It
holds the total quantity at each price, plus a private index of live orders
(side, price, total, remaining and seq), because cancel and replace events
name an order but not its price. The index is never published. The feed's
snapshot comes from this book and never from the sequencer. Tests compare it
with the aggregated engine book after every command.

The feed does not read time-in-force or order type. An arriving order rests
if it has quantity left after its trades and no `Cancelled` event for it
follows. An amend is in place if `Replaced.seq` equals the order's seq, and
otherwise the order leaves its level and arrives again.

## Not supported

- Per-order messages ("Level 3").
- Conflation: a slow client is dropped, never skipped ahead.
- Multicast, retransmission, replay from a sequence number.
- Authentication.
- Multiple instruments.
- Timestamps.
- Depth-limited subscriptions: a snapshot is always the whole book.
- Ordering between a REST acknowledgement and the feed message about the same
  command: neither is guaranteed to arrive first.
- `GET /book` is read from the sequencer and is not aligned with feed numbers.
