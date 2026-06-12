"""SessionHandle: the public seam below the UI line.

Layer: drive — the async shell; imports kernel + ports. All turn
choreography lives here: envelope stamping (one monotonic counter for
durable and transient alike), persist-before-run via the Bus, retry
wrapping, resume seeding. State is fold(log) on resume and the kernel's
live state otherwise — the same function, no duplicate message list
anywhere. The REPL above this line holds zero conversation state.

submit() runs one turn under a lock; subscribe() returns a bounded
AsyncIterator[Envelope]; answer_permission() feeds the built-in Asker
(used when no external Asker is injected); cancel() trips the turn's
cancel event; close() ends the session through the kernel (SessionEnded
lands in the log) and closes the store.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable

from forge.drive.bus import Bus, Subscription
from forge.drive.driver import Driver
from forge.drive.executor import ToolExecutor
from forge.drive.retry import RetryConfig, RetryingProvider
from forge.kernel.events import Event, make_envelope
from forge.kernel.state import SessionState, fold
from forge.kernel.step import Cancelled, Policy, UserInput
from forge.kernel.types import PermissionQuestion, TurnResult
from forge.ports.asker import Asker
from forge.ports.provider import Provider
from forge.ports.store import EventStore


class PendingAsker:
    """Asker that parks each question on a future until answer_permission().

    A subscriber reacting to the PermissionAsked envelope can answer before
    ask() has registered its future (publish happens first by design), so
    early answers are buffered by call_id; ids are unique per call, so a
    buffered answer can never approve a different call.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._early: dict[str, bool] = {}

    async def ask(self, question: PermissionQuestion) -> bool:
        if question.call_id in self._early:
            return self._early.pop(question.call_id)
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[question.call_id] = fut
        try:
            return await fut
        finally:
            self._pending.pop(question.call_id, None)

    def answer(self, call_id: str, allow: bool) -> bool:
        fut = self._pending.get(call_id)
        if fut is not None and not fut.done():
            fut.set_result(allow)
            return True
        self._early[call_id] = allow
        return False


class SessionHandle:
    def __init__(
        self,
        *,
        store: EventStore,
        provider: Provider,
        executor: ToolExecutor,
        policy: Policy,
        asker: Asker | None = None,
        sid: str | None = None,
        parent: str | None = None,
        state: SessionState | None = None,
        retry: RetryConfig | None = None,
        rng: Callable[[], float] = random.random,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        bus_maxsize: int = 256,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._sid = sid if sid is not None else uuid.uuid4().hex
        self._parent = parent
        self._state = state if state is not None else SessionState()
        self._seq = store.next_seq()
        self._clock = clock
        self._bus = Bus(store, maxsize=bus_maxsize)
        self._cancel = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closed = False
        self._pending_asker = PendingAsker()
        retrying = RetryingProvider(
            provider,
            retry if retry is not None else RetryConfig(),
            rng=rng,
            sleep=sleep,
            on_retry=self._emit,
        )
        self._driver = Driver(
            provider=retrying,
            executor=executor,
            asker=asker if asker is not None else self._pending_asker,
            policy=policy,
            publish=self._emit,
            cancel=self._cancel,
        )

    @classmethod
    def open(cls, store: EventStore, **kwargs: object) -> SessionHandle:
        return cls(store=store, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def resume(cls, store: EventStore, **kwargs: object) -> SessionHandle:
        """Seed live state as fold(log): resume and live are the same function."""
        log = store.replay()
        state = fold(log)
        sid = kwargs.pop("sid", None) or (log[0].sid if log else None)
        parent = kwargs.pop("parent", None) or (log[0].parent if log else None)
        return cls(store=store, state=state, sid=sid, parent=parent, **kwargs)  # type: ignore[arg-type]

    @property
    def sid(self) -> str:
        return self._sid

    @property
    def state(self) -> SessionState:
        return self._state

    def _emit(self, event: Event) -> None:
        env = make_envelope(
            seq=self._seq, sid=self._sid, body=event, parent=self._parent, ts=self._clock()
        )
        self._seq += 1
        self._bus.publish(env)

    async def submit(self, text: str) -> TurnResult | None:
        if self._closed:
            raise RuntimeError("session is closed")
        async with self._lock:
            self._cancel.clear()
            report = await self._driver.run_turn(self._state, UserInput(text))
            self._state = report.state
            if report.error is not None:
                raise report.error
            return report.result

    def subscribe(self, *, maxsize: int | None = None) -> Subscription:
        return self._bus.subscribe(maxsize=maxsize)

    def answer_permission(self, call_id: str, allow: bool) -> bool:
        return self._pending_asker.answer(call_id, allow)

    def cancel(self) -> None:
        self._cancel.set()

    async def close(self) -> None:
        if self._closed:
            return
        self._cancel.set()  # unblock any in-flight turn before taking the lock
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel.clear()
            if not self._state.finished:
                report = await self._driver.run_turn(self._state, Cancelled())
                self._state = report.state
            self._bus.close()
            await self._store.close()
