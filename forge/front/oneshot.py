"""Oneshot: drive one turn, render it, optionally emit the run record.

Layer: front — the `forge run -p ...` mode. With --json the TurnFinished-
derived run record goes to stdout as one JSON object — THE eval contract:

    {"sid": str, "outcome": "ok|aborted|max_turns|fatal", "turns": int,
     "usage": {"input_tokens", "output_tokens",
               "cache_read_tokens", "cache_write_tokens"},
     "cost": float | null, "error": str | null}

"turns" counts model rounds within the single user turn. Cost is computed
here from injected Pricing (the kernel logs cost as None); null when the
model is unpriced — never silently wrong. The non-interactive Asker is
StaticAsker: oneshot applies a fixed permission policy, deny by default.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from typing import TextIO

from forge.drive.session import SessionHandle
from forge.front.render import Renderer
from forge.kernel.events import TurnFinished
from forge.kernel.types import PermissionQuestion, Pricing


class StaticAsker:
    """Non-interactive permission policy: one fixed answer for every Ask."""

    def __init__(self, allow: bool = False) -> None:
        self.allow = allow
        self.questions: list[PermissionQuestion] = []

    async def ask(self, question: PermissionQuestion) -> bool:
        self.questions.append(question)
        return self.allow


async def run_once(
    handle: SessionHandle,
    prompt: str,
    *,
    renderer: Renderer,
    json_out: bool = False,
    out: TextIO | None = None,
    pricing: Pricing | None = None,
) -> int:
    """One turn against an already-composed SessionHandle; returns exit code."""
    out = out if out is not None else sys.stdout
    sub = handle.subscribe()
    finished: TurnFinished | None = None

    async def consume() -> None:
        nonlocal finished
        async for env in sub:
            renderer.handle(env)
            if isinstance(env.body, TurnFinished):
                finished = env.body

    consumer = asyncio.create_task(consume())
    error: BaseException | None = None
    result = None
    try:
        result = await handle.submit(prompt)
    except Exception as exc:  # noqa: BLE001 — turn state is committed; report and exit
        error = exc
    finally:
        await handle.close()  # ends the bus; the consumer drains then stops
        await consumer

    if error is not None:
        print(f"forge: {type(error).__name__}: {error}", file=sys.stderr)

    if finished is not None:
        outcome, usage = finished.outcome, finished.usage
    elif result is not None:
        outcome, usage = result.outcome, handle.state.turn_usage
    else:
        outcome, usage = "fatal", handle.state.turn_usage
    cost = finished.cost if finished is not None else None
    if cost is None and pricing is not None:
        cost = pricing.cost(usage)

    if json_out:
        record = {
            "sid": handle.sid,
            "outcome": outcome,
            "turns": handle.state.round,
            "usage": asdict(usage),
            "cost": cost,
            "error": str(error) if error is not None else None,
        }
        out.write(json.dumps(record) + "\n")
        out.flush()
    return 0 if error is None and outcome == "ok" else 1
