"""drive/session.py: SessionHandle — open/resume/submit/subscribe/close."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import SimPolicy, assert_matched_pairs, mk_out
from test_drive_driver import BlockingTool, TimedTool
from test_drive_executor import StubWorkspace

from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.kernel.events import (
    Envelope,
    PermissionAsked,
    SessionEnded,
    TextDelta,
    TurnFinished,
)
from forge.kernel.state import fold
from forge.kernel.types import Effects, TextBlock, ToolCall, Usage
from forge.testing import FakeProvider, MemoryStore


def handle_for(
    tmp_path: Path,
    script: list,
    tools=(),
    *,
    store: MemoryStore | None = None,
    verdicts: dict[str, str] | None = None,
    sid: str | None = None,
) -> tuple[SessionHandle, MemoryStore]:
    store = store if store is not None else MemoryStore()
    handle = SessionHandle.open(
        store,
        provider=FakeProvider(script),
        executor=ToolExecutor(tools, StubWorkspace(tmp_path)),
        policy=SimPolicy(verdicts=verdicts),
        sid=sid,
    )
    return handle, store


def tc(cid: str, name: str) -> ToolCall:
    return ToolCall(id=cid, name=name, args={})


async def test_submit_returns_result_and_folds_back(tmp_path: Path) -> None:
    handle, store = handle_for(tmp_path, [mk_out(TextBlock("hello"))])
    result = await handle.submit("hi")
    assert result is not None and result.outcome == "ok" and result.text == "hello"
    assert fold(store.replay()) == handle.state
    assert handle.state.turn == 1


async def test_subscribe_sees_durable_and_transient(tmp_path: Path) -> None:
    handle, store = handle_for(tmp_path, [mk_out(TextBlock("streamed"))])
    sub = handle.subscribe()
    await handle.submit("hi")
    sub.close()
    seen: list[Envelope] = [env async for env in sub]
    assert any(isinstance(env.body, TextDelta) for env in seen)  # transient delivered
    assert not any(isinstance(env.body, TextDelta) for env in store.replay())
    durable_seen = [env for env in seen if env.durable]
    assert durable_seen == list(store.replay())  # same envelopes, same order


async def test_envelopes_stamped_monotonic_with_sid(tmp_path: Path) -> None:
    handle, store = handle_for(tmp_path, [mk_out(TextBlock("a")), mk_out(TextBlock("b"))], sid="sid-1")
    sub = handle.subscribe()
    await handle.submit("one")
    await handle.submit("two")
    sub.close()
    seen = [env async for env in sub]
    seqs = [env.seq for env in seen]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert {env.sid for env in seen} == {"sid-1"}


async def test_resume_is_fold_of_log(tmp_path: Path) -> None:
    handle, store = handle_for(tmp_path, [mk_out(TextBlock("first"))], sid="sid-r")
    await handle.submit("turn one")
    live = handle.state

    resumed = SessionHandle.resume(
        store,
        provider=FakeProvider([mk_out(TextBlock("second"))]),
        executor=ToolExecutor((), StubWorkspace(tmp_path)),
        policy=SimPolicy(),
    )
    assert resumed.state == live  # resume == live: same fold
    assert resumed.sid == "sid-r"
    result = await resumed.submit("turn two")
    assert result is not None and result.outcome == "ok"
    assert resumed.state.turn == 2
    assert fold(store.replay()) == resumed.state


async def test_resume_seq_continues_monotonic(tmp_path: Path) -> None:
    handle, store = handle_for(tmp_path, [mk_out(TextBlock("a"))])
    await handle.submit("one")
    last = store.replay()[-1].seq
    resumed = SessionHandle.resume(
        store,
        provider=FakeProvider([mk_out(TextBlock("b"))]),
        executor=ToolExecutor((), StubWorkspace(tmp_path)),
        policy=SimPolicy(),
    )
    await resumed.submit("two")
    new_seqs = [env.seq for env in store.replay()[len(store.replay()) - 4 :]]
    assert all(s > last for s in new_seqs)


async def test_answer_permission_resolves_builtin_asker(tmp_path: Path) -> None:
    log: list = []
    tools = (TimedTool("w1", Effects.WRITE_PATH, log),)
    handle, store = handle_for(
        tmp_path,
        [mk_out(tc("c1", "w1")), mk_out(TextBlock("done"))],
        tools,
        verdicts={"c1": "ask"},
    )
    sub = handle.subscribe()
    turn = asyncio.create_task(handle.submit("go"))
    async for env in sub:
        if isinstance(env.body, PermissionAsked):
            # may race ask()'s future registration; buffered answers cover that
            handle.answer_permission(env.body.question.call_id, True)
            break
    result = await asyncio.wait_for(turn, 2)
    assert result is not None and result.outcome == "ok"
    assert ("end", "w1") in log
    assert fold(store.replay()) == handle.state
    assert_matched_pairs(handle.state)


async def test_answer_permission_before_ask_is_buffered(tmp_path: Path) -> None:
    handle, _ = handle_for(tmp_path, [])
    # False: nothing was waiting yet — the answer is buffered for its call_id
    assert handle.answer_permission("c-early", True) is False
    from forge.kernel.types import PermissionQuestion

    asker = handle._pending_asker
    assert await asker.ask(PermissionQuestion("c-early", "w1", "allow?")) is True


async def test_cancel_aborts_turn(tmp_path: Path) -> None:
    tool = BlockingTool()
    handle, store = handle_for(tmp_path, [mk_out(tc("c1", "block"))], (tool,))
    turn = asyncio.create_task(handle.submit("go"))
    await asyncio.wait_for(tool.started.wait(), 1)
    handle.cancel()
    result = await asyncio.wait_for(turn, 2)
    assert result is not None and result.outcome == "aborted"
    assert fold(store.replay()) == handle.state
    assert_matched_pairs(handle.state)


async def test_close_logs_session_ended_and_closes_store(tmp_path: Path) -> None:
    handle, store = handle_for(tmp_path, [mk_out(TextBlock("hi"))])
    sub = handle.subscribe()
    await handle.submit("go")
    await handle.close()
    assert isinstance(store.replay()[-1].body, SessionEnded)
    assert store.closed
    assert handle.state.finished
    seen = [env async for env in sub]  # close ended the subscription
    assert isinstance(seen[-1].body, SessionEnded)
    with pytest.raises(RuntimeError):
        await handle.submit("again")
    await handle.close()  # idempotent


async def test_usage_accumulates_across_turns(tmp_path: Path) -> None:
    script = [
        mk_out(TextBlock("a"), usage=Usage(10, 5)),
        mk_out(TextBlock("b"), usage=Usage(20, 7)),
    ]
    handle, store = handle_for(tmp_path, script)
    await handle.submit("one")
    await handle.submit("two")
    assert handle.state.usage == Usage(30, 12)
    finished = [env.body for env in store.replay() if isinstance(env.body, TurnFinished)]
    assert [f.usage for f in finished] == [Usage(10, 5), Usage(20, 7)]
    assert fold(store.replay()) == handle.state


async def test_provider_failure_surfaces_but_log_stays_well_formed(tmp_path: Path) -> None:
    from forge.ports.provider import FatalProviderError

    handle, store = handle_for(tmp_path, [FatalProviderError("invalid request")])
    with pytest.raises(FatalProviderError):
        await handle.submit("go")
    assert not handle.state.in_turn
    assert isinstance(store.replay()[-1].body, TurnFinished)
    assert fold(store.replay()) == handle.state
