"""Event bus: per-subscriber bounded queues in front of the EventStore.

Layer: drive — the async shell; imports kernel + ports. publish() is
synchronous and never awaits: a durable envelope is appended to the store
FIRST (persist-before-run — no subscriber can observe an unpersisted fact),
then fanned out to every subscriber's bounded queue. Transient envelopes
skip the store and are bus-only.

Overflow policy: drop-oldest. A slow subscriber loses its oldest
undelivered envelopes rather than stalling the loop; Subscription.dropped
counts the loss. Chosen over backpressure because every durable envelope
is already on disk by delivery time — a subscriber that fell behind can
always re-sync from the store, so blocking the agent to spare a renderer
buys nothing.
"""

from __future__ import annotations

import asyncio

from forge.kernel.events import Envelope
from forge.ports.store import EventStore

_CLOSE = object()  # queue sentinel: end of stream


class Subscription:
    """One subscriber's bounded view of the bus; an AsyncIterator[Envelope]."""

    def __init__(self, bus: Bus, maxsize: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize)
        self.dropped = 0
        self._done = False

    def _push(self, item: object) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass  # raced with the consumer; the put will now succeed

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Envelope:
        if self._done:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _CLOSE:
            self._done = True
            raise StopAsyncIteration
        return item  # type: ignore[return-value]

    def close(self) -> None:
        """Detach from the bus; already-queued envelopes still drain first."""
        self._bus._detach(self)
        self._push(_CLOSE)


class Bus:
    def __init__(self, store: EventStore, *, maxsize: int = 256) -> None:
        self._store = store
        self._maxsize = maxsize
        self._subs: list[Subscription] = []
        self._closed = False

    def publish(self, env: Envelope) -> None:
        if self._closed:
            raise RuntimeError("bus is closed")
        if env.durable:
            self._store.append(env)  # persist BEFORE any subscriber sees it
        for sub in tuple(self._subs):
            sub._push(env)

    def subscribe(self, *, maxsize: int | None = None) -> Subscription:
        if self._closed:
            raise RuntimeError("bus is closed")
        sub = Subscription(self, maxsize if maxsize is not None else self._maxsize)
        self._subs.append(sub)
        return sub

    def _detach(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    async def wait_empty(self) -> None:
        """Wait until every subscriber queue has delivered what is currently queued.

        Used so a REPL prompt cannot appear before the renderer has handled
        TurnFinished. Empty bus (no subs, or already drained) returns immediately.
        """
        while True:
            if all(sub._queue.empty() for sub in tuple(self._subs)):
                return
            await asyncio.sleep(0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in tuple(self._subs):
            sub._push(_CLOSE)
        self._subs.clear()
