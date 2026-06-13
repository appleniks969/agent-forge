"""LOAD-BEARING: fold(store.replay()) == live state through SessionHandle.

Scripted multi-turn scenarios (tool batches, a denial, an ask, a cancel, a
transient retry) run through the full drive stack; after each we assert that
folding the persisted log reproduces the live kernel state, and that every
reachable state (every prefix of the log) keeps tool_use/tool_result pairs
matched. A hypothesis property randomizes tool completion order and the ask
verdict to show the persisted log is deterministic under concurrency.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from conftest import SimPolicy, assert_matched_pairs, mk_out
from hypothesis import given, settings
from hypothesis import strategies as st
from test_drive_driver import BlockingTool, ScriptedAsker, TimedTool
from test_drive_executor import StubWorkspace

from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.kernel.events import Envelope, RetryScheduled, ToolFinished, TurnFinished
from forge.kernel.state import fold
from forge.kernel.types import (
    AssistantMessage,
    Effects,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
)
from forge.ports.provider import TransientProviderError
from forge.ports.tool import ToolCtx
from forge.testing import FakeProvider, MemoryStore


def tc(cid: str, name: str) -> ToolCall:
    return ToolCall(id=cid, name=name, args={})


def assert_replay_invariants(store: MemoryStore, handle: SessionHandle) -> None:
    """fold == live, and matched tool pairs hold in every reachable state."""
    log = store.replay()
    assert fold(log) == handle.state
    for i in range(len(log) + 1):
        _assert_pairs_in_prefix(fold(log[:i]), log[:i])


def _assert_pairs_in_prefix(state, prefix: tuple[Envelope, ...]) -> None:
    msgs = state.messages
    for j, msg in enumerate(msgs):
        if isinstance(msg, AssistantMessage) and msg.tool_calls and j + 1 < len(msgs):
            nxt = msgs[j + 1]
            assert isinstance(nxt, ToolResultMessage), f"prefix {len(prefix)}: msg {j + 1}"
            assert [r.call_id for r in nxt.results] == [c.id for c in msg.tool_calls]
    if msgs and isinstance(msgs[-1], AssistantMessage) and msgs[-1].tool_calls:
        # batch in flight: everything declared so far belongs to that batch
        assert {c.id for c in state.declared} <= {c.id for c in msgs[-1].tool_calls}


def open_handle(
    tmp_path: Path,
    script: list,
    tools=(),
    *,
    verdicts: dict[str, str] | None = None,
    asker=None,
    rng=None,
    sleep=None,
) -> tuple[SessionHandle, MemoryStore]:
    store = MemoryStore()
    kwargs: dict[str, Any] = {}
    if rng is not None:
        kwargs["rng"] = rng
    if sleep is not None:
        kwargs["sleep"] = sleep
    handle = SessionHandle.open(
        store,
        provider=FakeProvider(script),
        executor=ToolExecutor(tools, StubWorkspace(tmp_path)),
        policy=SimPolicy(verdicts=verdicts),
        asker=asker,
        **kwargs,
    )
    return handle, store


async def test_multi_turn_tool_batches(tmp_path: Path) -> None:
    log: list = []
    tools = (
        TimedTool("ra", Effects.READ_PATH, log, delay=0.01),
        TimedTool("rb", Effects.READ_PATH, log, delay=0.0),
        TimedTool("wr", Effects.WRITE_PATH, log, delay=0.0),
    )
    script = [
        mk_out(TextBlock("plan"), tc("c1", "ra"), tc("c2", "wr"), tc("c3", "rb")),
        mk_out(TextBlock("turn one done")),
        mk_out(tc("c4", "rb")),
        mk_out(TextBlock("turn two done")),
    ]
    handle, store = open_handle(tmp_path, script, tools)
    r1 = await handle.submit("first")
    assert r1 is not None and r1.outcome == "ok"
    assert_replay_invariants(store, handle)
    r2 = await handle.submit("second")
    assert r2 is not None and r2.outcome == "ok"
    assert_replay_invariants(store, handle)
    assert_matched_pairs(handle.state)
    assert handle.state.turn == 2


async def test_denial_scenario(tmp_path: Path) -> None:
    log: list = []
    tools = (
        TimedTool("ra", Effects.READ_PATH, log),
        TimedTool("wr", Effects.WRITE_PATH, log),
    )
    script = [mk_out(tc("ok1", "ra"), tc("no1", "wr")), mk_out(TextBlock("done"))]
    handle, store = open_handle(tmp_path, script, tools, verdicts={"no1": "deny"})
    result = await handle.submit("go")
    assert result is not None and result.outcome == "ok"
    assert ("end", "ra") in log and ("start", "wr") not in log
    denied = [
        env.body.result
        for env in store.replay()
        if isinstance(env.body, ToolFinished) and env.body.result.call_id == "no1"
    ]
    assert denied[0].is_error
    assert_replay_invariants(store, handle)
    assert_matched_pairs(handle.state)


async def test_ask_scenario_allow_and_deny(tmp_path: Path) -> None:
    log: list = []
    tools = (
        TimedTool("w1", Effects.WRITE_PATH, log),
        TimedTool("w2", Effects.EXEC, log),
    )
    script = [mk_out(tc("a1", "w1"), tc("a2", "w2")), mk_out(TextBlock("done"))]
    asker = ScriptedAsker({"a1": True, "a2": False})
    handle, store = open_handle(
        tmp_path, script, tools, verdicts={"a1": "ask", "a2": "ask"}, asker=asker
    )
    result = await handle.submit("go")
    assert result is not None and result.outcome == "ok"
    assert ("end", "w1") in log and ("start", "w2") not in log
    assert_replay_invariants(store, handle)
    assert_matched_pairs(handle.state)


async def test_cancel_scenario(tmp_path: Path) -> None:
    tool = BlockingTool()
    handle, store = open_handle(tmp_path, [mk_out(tc("c1", "block"))], (tool,))
    turn = asyncio.create_task(handle.submit("go"))
    await asyncio.wait_for(tool.started.wait(), 1)
    handle.cancel()
    result = await asyncio.wait_for(turn, 2)
    assert result is not None and result.outcome == "aborted"
    assert_replay_invariants(store, handle)
    assert_matched_pairs(handle.state)
    # placeholder result is in the log, so replay reproduces the abort
    placeholders = [
        env.body.result for env in store.replay() if isinstance(env.body, ToolFinished)
    ]
    assert placeholders == [ToolResult("c1", "cancelled", is_error=True)]


async def test_transient_retry_scenario(tmp_path: Path) -> None:
    async def no_sleep(_: float) -> None:
        return None

    script = [TransientProviderError("overloaded"), mk_out(TextBlock("recovered"))]
    handle, store = open_handle(tmp_path, script, rng=lambda: 1.0, sleep=no_sleep)
    result = await handle.submit("go")
    assert result is not None and result.outcome == "ok" and result.text == "recovered"
    retries = [env.body for env in store.replay() if isinstance(env.body, RetryScheduled)]
    assert len(retries) == 1 and "overloaded" in retries[0].reason
    assert_replay_invariants(store, handle)


async def test_close_after_scenarios_keeps_fold_equal(tmp_path: Path) -> None:
    handle, store = open_handle(tmp_path, [mk_out(TextBlock("hi"))])
    await handle.submit("go")
    await handle.close()
    assert fold(store.replay()) == handle.state
    # An idle close leaves the session resumable (not finished); the log ends
    # after its last TurnFinished and fold == live still holds.
    assert handle.state.finished is False
    assert isinstance(store.replay()[-1].body, TurnFinished)


# --- hypothesis: interleavings -------------------------------------------------


class GatedTool:
    """Completes only when its gate is released — completion order is scripted."""

    def __init__(self, name: str) -> None:
        self.spec = ToolSpec(
            name=name, description=name, params={"type": "object"}, effects=Effects.READ_PATH
        )
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        self.started.set()
        await self.gate.wait()
        return ToolResult("", f"{self.spec.name} done")


async def _interleaved_run(
    tmp_path: Path, order: list[int], allow_write: bool
) -> tuple[list[str], list[str], object, object]:
    """One turn: 3 gated reads completing in `order`, plus one ask'd write."""
    reads = [GatedTool(f"r{i}") for i in range(3)]
    log: list = []
    write = TimedTool("wr", Effects.WRITE_PATH, log)
    script = [
        mk_out(tc("c0", "r0"), tc("c1", "r1"), tc("c2", "r2"), tc("cw", "wr")),
        mk_out(TextBlock("done")),
    ]
    handle, store = open_handle(
        tmp_path,
        script,
        (*reads, write),
        verdicts={"cw": "ask"},
        asker=ScriptedAsker({"cw": allow_write}),
    )

    async def release() -> None:
        for tool in reads:
            await tool.started.wait()
        for i in order:
            reads[i].gate.set()
            await asyncio.sleep(0)

    releaser = asyncio.create_task(release())
    result = await asyncio.wait_for(handle.submit("go"), 5)
    await releaser
    assert result is not None and result.outcome == "ok"
    assert_replay_invariants(store, handle)
    assert_matched_pairs(handle.state)
    durable_kinds = [type(env.body).__name__ for env in store.replay()]
    finished_ids = [
        env.body.result.call_id for env in store.replay() if isinstance(env.body, ToolFinished)
    ]
    return durable_kinds, finished_ids, fold(store.replay()), handle.state


@settings(max_examples=20, deadline=None)
@given(order=st.permutations(list(range(3))), allow_write=st.booleans())
def test_log_deterministic_under_interleavings(order: list[int], allow_write: bool) -> None:
    async def scenario() -> None:
        # the workspace root is never written by these stub tools; any dir works
        with tempfile.TemporaryDirectory() as root:
            baseline = await _interleaved_run(Path(root), [0, 1, 2], allow_write)
            shuffled = await _interleaved_run(Path(root), list(order), allow_write)
        # completion order on the wire must not change the persisted history
        assert shuffled[0] == baseline[0]  # same durable event kinds, same order
        assert shuffled[1] == baseline[1]  # outcomes re-entered in call-index order
        assert shuffled[2] == shuffled[3]  # fold == live for the shuffled run

    asyncio.run(scenario())


async def test_turn_finished_terminates_every_scenario_log(tmp_path: Path) -> None:
    """Each submitted turn lands exactly one TurnFinished — the fold boundary."""
    log: list = []
    tools = (TimedTool("ra", Effects.READ_PATH, log),)
    script = [
        mk_out(tc("c1", "ra")),
        mk_out(TextBlock("one")),
        mk_out(TextBlock("two")),
    ]
    handle, store = open_handle(tmp_path, script, tools)
    await handle.submit("first")
    await handle.submit("second")
    finishes = [env.body for env in store.replay() if isinstance(env.body, TurnFinished)]
    assert len(finishes) == 2
    assert [f.outcome for f in finishes] == ["ok", "ok"]
    assert_replay_invariants(store, handle)
