"""EventStore port: the append-only log seam.

Layer: ports — Protocols over kernel types only, no logic. Stores persist
durable envelopes (fsync before anything else sees them); replay feeds fold,
so resume == live. next_seq() hands out the next sequence number so the
caller can stamp envelopes monotonically.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from forge.kernel.events import Envelope


class EventStore(Protocol):
    def append(self, env: Envelope) -> None: ...

    def replay(self) -> Sequence[Envelope]: ...

    def next_seq(self) -> int: ...

    async def close(self) -> None: ...
