"""REPL: a renderer plus Asker over SessionHandle.

Layer: front — holds ZERO conversation state; the SessionHandle below the
UI line owns choreography and the log owns truth. On a TTY, input uses
prompt_toolkit (history + paste collapse). Tests and pipes inject input_fn
via asyncio.to_thread. Slash commands dispatch through the shared table;
/clear closes the session and asks the injected factory for a fresh one.
Ctrl+C during a turn cancels it; the next prompt waits until the bus drains.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TextIO

from forge.adapters.mcp.manager import MCPManager
from forge.adapters.skills import SkillMeta
from forge.drive.bus import Subscription
from forge.drive.session import SessionHandle
from forge.front import commands
from forge.front.render import Renderer
from forge.kernel.types import PermissionQuestion, Pricing

PROMPT = "forge> "

InputFn = Callable[[str], str]
SessionFactory = Callable[[], Awaitable[SessionHandle]]

_LARGE_PASTE_LINES = 10
_PASTE_RE = re.compile(r"\[\+ \d+ lines pasted\]")


class _PasteStore:
    """Collapses large pastes to a `[+ N lines pasted]` marker and expands the
    markers back to their content on submit (FIFO: markers expand in order)."""

    def __init__(self) -> None:
        self._queue: list[str] = []

    def marker_for(self, data: str) -> str:
        self._queue.append(data)
        return f"[+ {data.count(chr(10)) + 1} lines pasted]"

    def expand(self, text: str) -> str:
        def _sub(_m: re.Match[str]) -> str:
            return self._queue.pop(0) if self._queue else _m.group(0)

        out = _PASTE_RE.sub(_sub, text)
        self._queue.clear()
        return out


def _ptk_reader(store: _PasteStore) -> Callable[[str], Awaitable[str]] | None:
    """A prompt_toolkit reader with bracketed-paste collapse + history, or None
    when stdin is not a TTY (tests / pipes fall back to the injected input_fn)."""
    if not sys.stdin.isatty():
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
    except ImportError:
        return None

    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste)
    def _on_paste(event) -> None:  # noqa: ANN001 — ptk event type
        data = event.data
        if data.count("\n") + 1 > _LARGE_PASTE_LINES:
            event.current_buffer.insert_text(store.marker_for(data))
        else:
            event.current_buffer.insert_text(data)

    hist_path = Path.home() / ".forge" / "history"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(hist_path)), key_bindings=kb
    )

    async def read(prompt: str) -> str:
        return await session.prompt_async(prompt)

    return read


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
    last_dropped = 0
    async for env in sub:
        renderer.handle(env)
        if sub.dropped > last_dropped:
            renderer.note_dropped(sub.dropped - last_dropped)
            last_dropped = sub.dropped


async def run_repl(
    make_session: SessionFactory,
    *,
    model: str,
    pricing: Pricing | None = None,
    input_fn: InputFn | None = None,
    out: TextIO | None = None,
    mcp: MCPManager | None = None,
    cwd: Path | None = None,
    skills: Sequence[SkillMeta] | Callable[[], str] | None = None,
    skill_resolver: Callable[[str], str | None] | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    renderer = Renderer(out=out, pricing=pricing)
    handle = await make_session()
    sub = handle.subscribe()
    consumer = asyncio.create_task(_consume(sub, renderer))

    # Input: an explicit input_fn (tests) reads via a thread; otherwise the
    # real REPL uses prompt_toolkit with paste collapse + history.
    paste = _PasteStore()
    ptk_read = _ptk_reader(paste) if input_fn is None else None
    fallback = input_fn if input_fn is not None else input

    async def read_line() -> str:
        if ptk_read is not None:
            return paste.expand(await ptk_read(PROMPT))
        return await asyncio.to_thread(fallback, PROMPT)

    renderer.print_banner(model, commands="/help  /skills  /status  /mcp  /clear  /quit")
    try:
        while True:
            try:
                line = await read_line()
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                ctx = commands.CommandContext(
                    session=handle,
                    model=model,
                    mcp=mcp,
                    cwd=cwd,
                    skills=skills,
                    skill_resolver=skill_resolver,
                )
                outcome = commands.dispatch(line, ctx)
                if outcome.text:
                    print(outcome.text, file=out)
                if outcome.action is not None:
                    print(await outcome.action(), file=out)
                if outcome.quit:
                    break
                if outcome.clear:
                    await handle.close()
                    await consumer
                    handle = await make_session()
                    sub = handle.subscribe()
                    consumer = asyncio.create_task(_consume(sub, renderer))
                continue
            turn = asyncio.create_task(handle.submit(line))
            try:
                await turn
            except KeyboardInterrupt:
                handle.cancel()
                try:
                    await turn
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001 — shell survives turn failures
                print(f"forge: {type(exc).__name__}: {exc}", file=out)
            await handle.wait_until_idle()
    finally:
        await handle.close()
        await consumer
    return 0
