"""The dumb loop: feed Inputs to the kernel, execute its Effects.

Layer: drive — the async shell; imports kernel + ports. Razor: no branch
here keys on tool names, provider names, or event contents — that is
policy or kernel territory.

Per step the driver publishes every emitted event (durable envelopes hit
the store before any subscriber — persist-before-run) and only THEN
executes effects. CallModel goes through the injected provider (wrap it
with retry.RetryingProvider at composition time) with deltas published as
transient events; a completed output's Usage is fed back to a policy that
exposes observe_usage() so token estimates calibrate against reality
(spec 3.7). RunTools runs in one TaskGroup: calls whose Effects are
read-only run concurrently; anything WRITE_PATH|EXEC|EXTERNAL runs in a
single serializer task (serial among themselves, concurrent with reads —
the kernel re-enters outcomes in call-index order either way, so replay
is deterministic). AskUser resolves through the Asker port; Finish ends
the turn.

Cancellation is one asyncio.Event: every await races against it; when it
fires the driver abandons in-flight work and injects Cancelled so the
kernel does the placeholder bookkeeping. A provider error that escapes
retry closes the turn the same way (the log stays well-formed) and is
returned on the TurnReport for the caller to re-raise.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from forge.drive.executor import ToolExecutor
from forge.kernel.events import Event, TextDelta, ThinkingDelta
from forge.kernel.state import SessionState
from forge.kernel.step import (
    AskUser,
    CallModel,
    Cancelled,
    Finish,
    Input,
    ModelResponded,
    PermissionAnswer,
    Policy,
    RunTools,
    ToolOutcome,
    step,
)
from forge.kernel.types import Delta, Effects, ToolCall, ToolResult, TurnResult, Usage
from forge.ports.asker import Asker
from forge.ports.provider import FatalProviderError, Provider, TransientProviderError

SERIAL_EFFECTS = Effects.WRITE_PATH | Effects.EXEC | Effects.EXTERNAL

_CANCELLED = object()  # sentinel: the cancel event won the race


@runtime_checkable
class CalibratesFromUsage(Protocol):
    """Optional policy capability: recalibrate token estimates from the Usage
    of each completed ModelOutput. Deliberately outside the kernel Policy
    Protocol — estimation is policy-internal and the kernel never sees it."""

    def observe_usage(self, usage: Usage) -> None: ...


@dataclass(frozen=True)
class TurnReport:
    state: SessionState
    result: TurnResult | None
    error: BaseException | None = None


class Driver:
    def __init__(
        self,
        *,
        provider: Provider,
        executor: ToolExecutor,
        asker: Asker,
        policy: Policy,
        publish: Callable[[Event], None],
        cancel: asyncio.Event,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._asker = asker
        self._policy = policy
        self._publish = publish
        self._cancel = cancel

    async def run_turn(self, state: SessionState, inp: Input) -> TurnReport:
        queue: deque[Input] = deque([inp])
        result: TurnResult | None = None
        error: BaseException | None = None
        while queue:
            cur = queue.popleft()
            if self._cancel.is_set() and not isinstance(cur, Cancelled):
                queue.clear()
                cur = Cancelled()
            st = step(state, cur, self._policy)
            state = st.state
            for event in st.events:
                self._publish(event)
            interrupted = False
            for eff in st.effects:
                match eff:
                    case Finish(result=res):
                        result = res
                    case CallModel():
                        try:
                            out = await self._race(
                                self._provider.complete(eff.request, on_delta=self._on_delta)
                            )
                        except (TransientProviderError, FatalProviderError) as exc:
                            error = exc
                            interrupted = True
                            break
                        if out is _CANCELLED:
                            interrupted = True
                            break
                        if isinstance(self._policy, CalibratesFromUsage):
                            # Before the fold: stepping ModelResponded builds the
                            # next request, which would overwrite the estimate
                            # this Usage pairs with.
                            self._policy.observe_usage(out.usage)
                        queue.append(ModelResponded(output=out, request=eff.request))
                    case RunTools(calls=calls):
                        outcomes = await self._run_tools(calls)
                        if outcomes is _CANCELLED:
                            interrupted = True
                            break
                        queue.extend(ToolOutcome(result=r) for r in outcomes)
                    case AskUser(question=question):
                        allow = await self._race(self._asker.ask(question))
                        if allow is _CANCELLED:
                            interrupted = True
                            break
                        queue.append(PermissionAnswer(question.call_id, bool(allow)))
            if interrupted:
                queue.clear()
                queue.append(Cancelled())
        return TurnReport(state=state, result=result, error=error)

    def _on_delta(self, delta: Delta) -> None:
        event = ThinkingDelta(delta.text) if delta.kind == "thinking" else TextDelta(delta.text)
        self._publish(event)

    async def _race(self, coro: Any) -> Any:
        """Run coro to completion unless the cancel event fires first."""
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(self._cancel.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 — abandoned work; outcome is irrelevant
                pass
            return _CANCELLED
        finally:
            waiter.cancel()

    async def _run_tools(self, calls: tuple[ToolCall, ...]) -> list[ToolResult] | object:
        results: dict[int, ToolResult] = {}

        async def run_one(i: int, call: ToolCall) -> None:
            results[i] = await self._executor.execute(call, self._cancel)

        async def run_serial(items: list[tuple[int, ToolCall]]) -> None:
            for i, call in items:
                results[i] = await self._executor.execute(call, self._cancel)

        serial = [
            (i, c)
            for i, c in enumerate(calls)
            if self._executor.effects_of(c.name) & SERIAL_EFFECTS
        ]
        parallel = [
            (i, c)
            for i, c in enumerate(calls)
            if not (self._executor.effects_of(c.name) & SERIAL_EFFECTS)
        ]

        async def batch() -> None:
            async with asyncio.TaskGroup() as tg:
                for i, call in parallel:
                    tg.create_task(run_one(i, call))
                if serial:
                    tg.create_task(run_serial(serial))

        out = await self._race(batch())
        if out is _CANCELLED:
            return _CANCELLED
        # Re-enter in call-index order: deterministic replay under concurrency.
        return [results[i] for i in range(len(calls))]
