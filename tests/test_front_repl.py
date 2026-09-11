"""REPL: footer-before-prompt drain and mid-turn cancel."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from conftest import SimPolicy, mk_out
from test_drive_driver import BlockingTool
from test_drive_executor import StubWorkspace

from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.front.repl import run_repl
from forge.kernel.events import TurnFinished
from forge.kernel.types import TextBlock, ToolCall
from forge.testing import FakeProvider, MemoryStore


def tc(cid: str, name: str) -> ToolCall:
    return ToolCall(id=cid, name=name, args={})


async def test_repl_footer_is_visible_before_next_prompt(tmp_path: Path) -> None:
    out = io.StringIO()
    prompts = iter(["hello", "/quit"])
    snapshots: list[str] = []

    def input_fn(prompt: str) -> str:
        snapshots.append(out.getvalue())
        return next(prompts)

    async def make_session() -> SessionHandle:
        return SessionHandle.open(
            MemoryStore(),
            provider=FakeProvider([mk_out(TextBlock("hello from forge"))]),
            executor=ToolExecutor((), StubWorkspace(tmp_path)),
            policy=SimPolicy(),
        )

    await run_repl(make_session, model="fake", input_fn=input_fn, out=out)
    assert len(snapshots) >= 2
    # Second prompt is requested only after the turn drained.
    assert "ok" in snapshots[1]
    assert "hello from forge" in snapshots[1]


async def test_repl_cancel_aborts_in_flight_turn(tmp_path: Path) -> None:
    tool = BlockingTool()
    store = MemoryStore()
    out = io.StringIO()
    handle_holder: list[SessionHandle] = []
    started = asyncio.Event()

    async def make_and_keep() -> SessionHandle:
        h = SessionHandle.open(
            store,
            provider=FakeProvider([mk_out(tc("c1", "block"))]),
            executor=ToolExecutor((tool,), StubWorkspace(tmp_path)),
            policy=SimPolicy(),
        )
        handle_holder.append(h)
        return h

    def input_fn(prompt: str) -> str:
        if not started.is_set():
            started.set()
            return "go"
        raise EOFError

    async def cancel_when_blocked() -> None:
        await asyncio.wait_for(tool.started.wait(), 2)
        handle_holder[0].cancel()

    watcher = asyncio.create_task(cancel_when_blocked())
    await run_repl(make_and_keep, model="fake", input_fn=input_fn, out=out)
    await watcher
    assert any(
        isinstance(e.body, TurnFinished) and e.body.outcome == "aborted"
        for e in store.replay()
    )
