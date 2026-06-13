"""MemoryStore: an in-memory EventStore for tests.

Layer: testing — imports ports + kernel only. Persists durable envelopes
only, mirroring the real store's bus-only treatment of transients.
"""

from __future__ import annotations

from forge.kernel.events import Envelope


class MemoryStore:
    def __init__(self) -> None:
        self._log: list[Envelope] = []
        self.closed = False

    def append(self, env: Envelope) -> None:
        if env.durable:
            self._log.append(env)

    def replay(self) -> tuple[Envelope, ...]:
        return tuple(self._log)

    def next_seq(self) -> int:
        return self._log[-1].seq + 1 if self._log else 0

    async def close(self) -> None:
        self.closed = True
