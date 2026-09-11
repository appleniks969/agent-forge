"""front/commands + repl: the declarative slash table, dispatch semantics,
the ConsoleAsker, and a scripted end-to-end REPL session."""

from __future__ import annotations

import io
from pathlib import Path

from forge.adapters.tools.workspace import RootedWorkspace
from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.front import commands, oneshot, repl
from forge.kernel.types import ModelOutput, PermissionQuestion, TextBlock, Usage
from forge.policy import StandardPolicy
from forge.testing import FakeProvider, MemoryStore


def make_handle(
    tmp_path: Path, script: list[ModelOutput], sid: str | None = None
) -> SessionHandle:
    ws = RootedWorkspace(tmp_path)
    return SessionHandle.open(
        MemoryStore(),
        provider=FakeProvider(script),
        executor=ToolExecutor((), ws),
        policy=StandardPolicy(model="m", context_tokens=200_000, tools=(), ws_root=ws.root),
        asker=oneshot.StaticAsker(allow=False),
        sid=sid,
    )


def make_ctx(tmp_path: Path) -> commands.CommandContext:
    handle = make_handle(tmp_path, [], sid="sid123")
    return commands.CommandContext(session=handle, model="model-x")


# --- the table -----------------------------------------------------------------


def test_table_has_required_commands() -> None:
    names = [c.name for c in commands.COMMANDS]
    assert {"quit", "status", "clear"} <= set(names)
    assert len(names) == len(set(names))  # no duplicate names
    assert all(c.help for c in commands.COMMANDS)


def test_dispatch_quit(tmp_path: Path) -> None:
    outcome = commands.dispatch("/quit", make_ctx(tmp_path))
    assert outcome.quit and not outcome.clear


def test_dispatch_clear(tmp_path: Path) -> None:
    outcome = commands.dispatch("/clear", make_ctx(tmp_path))
    assert outcome.clear and not outcome.quit


def test_dispatch_status_reports_session(tmp_path: Path) -> None:
    text = commands.dispatch("/status", make_ctx(tmp_path)).text
    assert "sid123" in text
    assert "model-x" in text
    assert "turns" in text
    assert "tokens" in text


def test_dispatch_help_lists_every_command(tmp_path: Path) -> None:
    text = commands.dispatch("/help", make_ctx(tmp_path)).text
    for cmd in commands.COMMANDS:
        assert f"/{cmd.name}" in text
        assert cmd.help in text


def test_dispatch_unknown_points_at_help(tmp_path: Path) -> None:
    outcome = commands.dispatch("/nope", make_ctx(tmp_path))
    assert "unknown" in outcome.text and "/help" in outcome.text
    assert not outcome.quit and not outcome.clear


# --- ConsoleAsker ----------------------------------------------------------------


async def test_console_asker_yes_variants() -> None:
    question = PermissionQuestion(call_id="c1", tool="t", question="ok?")
    assert await repl.ConsoleAsker(lambda _: "y").ask(question) is True
    assert await repl.ConsoleAsker(lambda _: "YES").ask(question) is True
    assert await repl.ConsoleAsker(lambda _: "").ask(question) is False
    assert await repl.ConsoleAsker(lambda _: "n").ask(question) is False


async def test_console_asker_eof_denies() -> None:
    def raise_eof(prompt: str) -> str:
        raise EOFError

    question = PermissionQuestion(call_id="c1", tool="t", question="ok?")
    assert await repl.ConsoleAsker(raise_eof).ask(question) is False


async def test_console_asker_uses_bound_prompt() -> None:
    seen: list[str] = []

    async def prompt_async(prompt: str) -> str:
        seen.append(prompt)
        return "y"

    asker = repl.ConsoleAsker(lambda _: "n")
    asker.bind_prompt(prompt_async)
    question = PermissionQuestion(call_id="c1", tool="t", question="ok?")
    assert await asker.ask(question) is True
    assert seen and "allow t?" in seen[0]


# --- the REPL shell ---------------------------------------------------------------


def scripted_input(lines: list[str]) -> repl.InputFn:
    queue = iter(lines)

    def fake_input(prompt: str) -> str:
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    return fake_input


async def test_repl_submits_then_quits(tmp_path: Path) -> None:
    out = io.StringIO()
    script = [ModelOutput(blocks=(TextBlock("hi there"),), usage=Usage(10, 5))]

    async def make_session() -> SessionHandle:
        return make_handle(tmp_path, list(script))

    rc = await repl.run_repl(
        make_session,
        model="model-x",
        input_fn=scripted_input(["hello", "/status", "/quit"]),
        out=out,
    )
    text = out.getvalue()
    assert rc == 0
    assert "hi there" in text  # streamed deltas reached the renderer
    assert "model-x" in text  # /status output
    assert "turns: 1" in text


async def test_repl_clear_starts_fresh_session(tmp_path: Path) -> None:
    out = io.StringIO()
    sids: list[str] = []

    async def make_session() -> SessionHandle:
        handle = make_handle(
            tmp_path,
            [ModelOutput(blocks=(TextBlock("resp"),), usage=Usage(10, 5))],
        )
        sids.append(handle.sid)
        return handle

    rc = await repl.run_repl(
        make_session,
        model="m",
        input_fn=scripted_input(["hello", "/clear", "hello again", "/quit"]),
        out=out,
    )
    assert rc == 0
    assert len(sids) == 2 and sids[0] != sids[1]
    assert out.getvalue().count("resp") == 2  # one turn per session succeeded


async def test_repl_survives_provider_failure(tmp_path: Path) -> None:
    out = io.StringIO()

    async def make_session() -> SessionHandle:
        return make_handle(tmp_path, [])  # exhausted script -> fatal on submit

    rc = await repl.run_repl(
        make_session,
        model="m",
        input_fn=scripted_input(["boom", "/quit"]),
        out=out,
    )
    assert rc == 0
    assert "FatalProviderError" in out.getvalue()


# --- paste collapse (regression: v1 had [+ N lines pasted]) ----------------------

def test_paste_store_collapses_and_expands():
    from forge.front.repl import _PasteStore

    store = _PasteStore()
    big = "\n".join(f"line {i}" for i in range(50))
    marker = store.marker_for(big)
    assert marker == "[+ 50 lines pasted]"
    # A line containing the marker expands back to the full pasted content.
    assert store.expand(f"see {marker} ok") == f"see {big} ok"


def test_paste_store_expands_markers_in_order():
    from forge.front.repl import _PasteStore

    store = _PasteStore()
    a = "\n".join(["a"] * 20)
    b = "\n".join(["b"] * 30)
    m1, m2 = store.marker_for(a), store.marker_for(b)
    assert store.expand(f"{m1} then {m2}") == f"{a} then {b}"



def test_banner_lists_remember_and_sessions() -> None:
    line = commands.banner_commands()
    assert "/remember" in line
    assert "/sessions" in line
    for cmd in commands.COMMANDS:
        assert f"/{cmd.name}" in line


def test_dispatch_sessions_lists_prompt(tmp_path: Path) -> None:
    from forge.adapters.jsonl_store import JsonlStore
    from forge.kernel.events import UserSubmitted, make_envelope

    root = tmp_path / "sessions"
    sid = "abc123def4567890"
    store = JsonlStore(root, sid, cwd=str(tmp_path))
    store.append(make_envelope(0, sid, UserSubmitted("hello from list")))
    ctx = commands.CommandContext(
        session=make_handle(tmp_path, [], sid="sid123"),
        model="model-x",
        cwd=tmp_path,
        sessions_root=root,
    )
    text = commands.dispatch("/sessions", ctx).text
    assert "abc123def456" in text
    assert "hello from list" in text


def test_dispatch_sessions_empty(tmp_path: Path) -> None:
    ctx = commands.CommandContext(
        session=make_handle(tmp_path, [], sid="sid123"),
        model="model-x",
        cwd=tmp_path,
        sessions_root=tmp_path / "sessions",
    )
    text = commands.dispatch("/sessions", ctx).text
    assert "no sessions" in text


def test_history_path_lives_under_agent_forge() -> None:
    assert repl.HISTORY_PATH.name == "history"
    assert repl.HISTORY_PATH.parent.name == ".agent-forge"
