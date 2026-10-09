"""A checksum of the book's state, from an explicit canonical encoding.

Two books with the same resting orders and the same next trade id encode to the
same bytes, and any difference in what the book will do next changes the bytes.
The encoding is written out here rather than taken from `hash()`, `repr`, JSON
or pickle, because those can change between Python versions or runs, and a
checksum compared across processes must not.

Layout. The encoding is UTF-8 text made of lines, each ending in "\\n". Fields
within a line are separated by one space. In the layout below `<int>` is a
decimal integer with no padding and `<text>` is a string written as its length
in UTF-8 bytes, a colon, then those bytes: the id `o1` is `2:o1` and `ordér-1`
is `8:ordér-1` (é takes two bytes). Because a length comes first, a string can
contain spaces, colons or newlines without making two different books encode
alike.

    book-v1
    next_trade_id <int>
    bids <number of levels>
    level <price> <number of orders>
    order <order_id> <participant_id> <seq> <total_quantity> <remaining>
    order ...
    level ...
    asks <number of levels>
    level ...

Levels and orders appear in the snapshot's order: best price first on each
side, and oldest first within a level. The bids section comes before the asks
section. Every level line is followed by exactly as many order lines as its
count says, and every side by as many levels, so the layout has one reading.

The first line is the version. A change to the layout gets a new version line,
so that checksums of different layouts are never compared by mistake.
"""

from __future__ import annotations

import hashlib

from orderbook_engine.engine.snapshot import BookSnapshot, LevelSnapshot

_VERSION = "book-v1"


def encode_snapshot(snapshot: BookSnapshot) -> bytes:
    """The canonical bytes of `snapshot`, in the layout above."""
    lines = [
        _VERSION,
        f"next_trade_id {snapshot.next_trade_id}",
        *_section_lines("bids", snapshot.bids),
        *_section_lines("asks", snapshot.asks),
    ]
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def state_checksum(snapshot: BookSnapshot) -> str:
    """The SHA-256 hex digest of the canonical encoding of `snapshot`."""
    return hashlib.sha256(encode_snapshot(snapshot)).hexdigest()


def _section_lines(name: str, levels: tuple[LevelSnapshot, ...]) -> list[str]:
    """The lines of the `bids` or the `asks` section.

    A header with the number of levels, then for each level its own line
    followed by one line per order.
    """
    lines = [f"{name} {len(levels)}"]
    for level in levels:
        lines.append(f"level {level.price} {len(level.orders)}")
        lines.extend(
            "order "
            f"{_text(order.order_id)} {_text(order.participant_id)} "
            f"{order.seq} {order.total_quantity} {order.remaining}"
            for order in level.orders
        )
    return lines


def _text(value: str) -> str:
    """`value` as its UTF-8 byte length, a colon and the value itself."""
    return f"{len(value.encode('utf-8'))}:{value}"
