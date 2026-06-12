"""REPL: a stdlib line shell — a renderer plus Asker over SessionHandle.

Layer: front — holds ZERO conversation state; the SessionHandle below the
UI line owns choreography and the log owns truth. Input goes through
asyncio.to_thread so the event loop (and the streaming renderer) stays live
while the user types; no prompt_toolkit. Slash commands dispatch through
the shared declarative table; /clear closes the session and asks the
injected factory for a fresh one.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import TextIO

from forge.drive.bus import Subscription
from forge.drive.session import SessionHandle
from forge.front import commands
from forge.front.render import Renderer
from forge.kernel.types import PermissionQuestion, Pricing

PROMPT = "forge> "

InputFn = Callable[[str], str]
SessionFactory = Callable[[], Awaitable[SessionHandle]]


class ConsoleAsker:
    """The REPL's Asker: y/n prompt on permission Ask, read off-loop."""

    def __init__(self, input_fn: InputFn = input) -> None:
        self._input = input_fn

    async def ask(self, question: PermissionQuestion) -> bool:
        try:
            answer = await asyncio.to_thread(
                self._input, f"allow {question.tool}? [y/N] "
            )
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}


async def _consume(sub: Subscription, renderer: Renderer) -> None:
    async for env in sub:
        renderer.handle(env)


async def run_repl(
    make_session: SessionFactory,
    *,
    model: str,
    pricing: Pricing | None = None,
    input_fn: InputFn = input,
    out: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    renderer = Renderer(out=out, pricing=pricing)
    handle = await make_session()
    sub = handle.subscribe()
    consumer = asyncio.create_task(_consume(sub, renderer))
    print(f"forge | {model} | /help for commands", file=out)
    try:
        while True:
            try:
                line = await asyncio.to_thread(input_fn, PROMPT)
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                ctx = commands.CommandContext(session=handle, model=model)
                outcome = commands.dispatch(line, ctx)
                if outcome.text:
                    print(outcome.text, file=out)
                if outcome.quit:
                    break
                if outcome.clear:
                    await handle.close()
                    await consumer
                    handle = await make_session()
                    sub = handle.subscribe()
                    consumer = asyncio.create_task(_consume(sub, renderer))
                continue
            try:
                await handle.submit(line)
            except Exception as exc:  # noqa: BLE001 — shell survives turn failures
                print(f"forge: {type(exc).__name__}: {exc}", file=out)
            # One yield lets the consumer drain already-published envelopes
            # before the next prompt is printed.
            await asyncio.sleep(0)
    finally:
        await handle.close()
        await consumer
    return 0
