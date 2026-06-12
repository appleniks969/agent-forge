"""drive/driver.py: the dumb loop — effects executed, events persisted first."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import SimPolicy, assert_matched_pairs, kinds, mk_out
from test_drive_executor import StubWorkspace

from forge.drive.driver import Driver, TurnReport
from forge.drive.executor import ToolExecutor
from forge.drive.retry import RetryConfig, RetryingProvider
from forge.kernel.events import (
    Event,
    PermissionAsked,
    PermissionDecided,
    RetryScheduled,
    TextDelta,
    ToolFinished,
    TurnFinished,
    is_durable,
)
from forge.kernel.state import SessionState
from forge.kernel.step import UserInput
from forge.kernel.types import (
    Effects,
    PermissionQuestion,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from forge.policy import StandardPolicy
from forge.ports.provider import FatalProviderError, TransientProviderError
from forge.ports.tool import ToolCtx
from forge.testing import FakeProvider


class TimedTool:
    def __init__(self, name: str, effects: Effects, log: list, delay: float = 0.0) -> None:
        self.spec = ToolSpec(name=name, description=name, params={"type": "object"}, effects=effects)
        self._log = log
        self._delay = delay

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        self._log.append(("start", self.spec.name))
        await asyncio.sleep(self._delay)
        self._log.append(("end", self.spec.name))
        return ToolResult("", f"{self.spec.name} ok")


class BlockingTool:
    """Never returns; the driver must abandon it on cancel."""

    def __init__(self, name: str = "block") -> None:
        self.spec = ToolSpec(name=name, description="blocks", params={"type": "object"}, effects=Effects.EXEC)
        self.started = asyncio.Event()

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ScriptedAsker:
    def __init__(self, answers: dict[str, bool]) -> None:
        self.answers = answers
        self.asked: list[PermissionQuestion] = []

    async def ask(self, question: PermissionQuestion) -> bool:
        self.asked.append(question)
        return self.answers[question.call_id]


def make_driver(
    tmp_path: Path,
    provider,
    tools=(),
    *,
    verdicts: dict[str, str] | None = None,
    asker=None,
    cancel: asyncio.Event | None = None,
) -> tuple[Driver, list[Event]]:
    events: list[Event] = []
    driver = Driver(
        provider=provider,
        executor=ToolExecutor(tools, StubWorkspace(tmp_path)),
        asker=asker if asker is not None else ScriptedAsker({}),
        policy=SimPolicy(verdicts=verdicts),
        publish=events.append,
        cancel=cancel if cancel is not None else asyncio.Event(),
    )
    return driver, events


def tc(cid: str, name: str) -> ToolCall:
    return ToolCall(id=cid, name=name, args={})


def phases(log: list, name: str) -> tuple[int, int]:
    return log.index(("start", name)), log.index(("end", name))


async def test_text_only_turn(tmp_path: Path) -> None:
    provider = FakeProvider([mk_out(TextBlock("hello"))])
    driver, events = make_driver(tmp_path, provider)
    report = await driver.run_turn(SessionState(), UserInput("hi"))
    assert report.result is not None and report.result.outcome == "ok"
    assert report.result.text == "hello"
    assert report.error is None
    durable = [e for e in events if is_durable(e)]
    assert kinds(durable) == ["UserSubmitted", "TurnStarted", "AssistantBlock", "TurnFinished"]
    assert not report.state.in_turn


async def test_deltas_published_as_transient_events(tmp_path: Path) -> None:
    provider = FakeProvider([mk_out(TextBlock("streamed"))])
    driver, events = make_driver(tmp_path, provider)
    await driver.run_turn(SessionState(), UserInput("hi"))
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert [d.text for d in deltas] == ["streamed"]


async def test_persist_before_run_ordering(tmp_path: Path) -> None:
    events: list[Event] = []
    snapshots: list[list[str]] = []

    class SnoopingProvider(FakeProvider):
        async def complete(self, req, on_delta=None):
            snapshots.append(kinds(events))
            return await super().complete(req, on_delta)

    provider = SnoopingProvider([mk_out(TextBlock("ok"))])
    driver = Driver(
        provider=provider,
        executor=ToolExecutor((), StubWorkspace(tmp_path)),
        asker=ScriptedAsker({}),
        policy=SimPolicy(),
        publish=events.append,
        cancel=asyncio.Event(),
    )
    await driver.run_turn(SessionState(), UserInput("hi"))
    # the durable events of the step were published before its effect ran
    assert snapshots[0] == ["UserSubmitted", "TurnStarted"]


async def test_reads_parallel_writes_serialized(tmp_path: Path) -> None:
    log: list = []
    tools = (
        TimedTool("ra", Effects.READ_PATH, log, delay=0.02),
        TimedTool("rb", Effects.READ_PATH, log, delay=0.02),
        TimedTool("w1", Effects.WRITE_PATH, log, delay=0.02),
        TimedTool("w2", Effects.EXEC, log, delay=0.02),
    )
    provider = FakeProvider(
        [mk_out(tc("c1", "ra"), tc("c2", "w1"), tc("c3", "rb"), tc("c4", "w2")), mk_out(TextBlock("done"))]
    )
    driver, events = make_driver(tmp_path, provider, tools)
    report = await driver.run_turn(SessionState(), UserInput("go"))
    assert report.result is not None and report.result.outcome == "ok"
    ra0, ra1 = phases(log, "ra")
    rb0, rb1 = phases(log, "rb")
    assert ra0 < rb1 and rb0 < ra1  # reads overlap
    w1s, w1e = phases(log, "w1")
    w2s, w2e = phases(log, "w2")
    assert w1e < w2s  # serial calls never overlap, and run in call order
    assert_matched_pairs(report.state)


async def test_outcomes_reenter_in_call_index_order(tmp_path: Path) -> None:
    log: list = []
    tools = (
        TimedTool("slowt", Effects.READ_PATH, log, delay=0.03),
        TimedTool("fastt", Effects.READ_PATH, log, delay=0.0),
    )
    provider = FakeProvider([mk_out(tc("s1", "slowt"), tc("f1", "fastt")), mk_out(TextBlock("done"))])
    driver, events = make_driver(tmp_path, provider, tools)
    report = await driver.run_turn(SessionState(), UserInput("go"))
    # fastt finished first on the wire, but outcomes re-enter by call index
    assert log.index(("end", "fastt")) < log.index(("end", "slowt"))
    finished = [e.result.call_id for e in events if isinstance(e, ToolFinished)]
    assert finished == ["s1", "f1"]
    assert_matched_pairs(report.state)


async def test_policy_deny_short_circuits_tool(tmp_path: Path) -> None:
    log: list = []
    tools = (TimedTool("w1", Effects.WRITE_PATH, log),)
    provider = FakeProvider([mk_out(tc("c1", "w1")), mk_out(TextBlock("done"))])
    driver, events = make_driver(tmp_path, provider, tools, verdicts={"c1": "deny"})
    report = await driver.run_turn(SessionState(), UserInput("go"))
    assert report.result is not None and report.result.outcome == "ok"
    assert log == []  # the tool never ran
    decided = [e for e in events if isinstance(e, PermissionDecided)]
    assert decided[0].allowed is False and decided[0].source == "policy"
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished[0].result.is_error


async def test_ask_allow_runs_tool(tmp_path: Path) -> None:
    log: list = []
    tools = (TimedTool("w1", Effects.WRITE_PATH, log),)
    provider = FakeProvider([mk_out(tc("c1", "w1")), mk_out(TextBlock("done"))])
    asker = ScriptedAsker({"c1": True})
    driver, events = make_driver(tmp_path, provider, tools, verdicts={"c1": "ask"}, asker=asker)
    report = await driver.run_turn(SessionState(), UserInput("go"))
    assert [q.call_id for q in asker.asked] == ["c1"]
    assert ("end", "w1") in log
    assert any(isinstance(e, PermissionAsked) for e in events)
    decided = [e for e in events if isinstance(e, PermissionDecided)]
    assert decided[0].allowed is True and decided[0].source == "user"
    assert_matched_pairs(report.state)


async def test_ask_deny_yields_error_result(tmp_path: Path) -> None:
    log: list = []
    tools = (TimedTool("w1", Effects.WRITE_PATH, log),)
    provider = FakeProvider([mk_out(tc("c1", "w1")), mk_out(TextBlock("done"))])
    asker = ScriptedAsker({"c1": False})
    driver, events = make_driver(tmp_path, provider, tools, verdicts={"c1": "ask"}, asker=asker)
    report = await driver.run_turn(SessionState(), UserInput("go"))
    assert log == []
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished[0].result.is_error and "denied by user" in finished[0].result.content
    assert_matched_pairs(report.state)


async def test_cancel_mid_tool_injects_placeholders(tmp_path: Path) -> None:
    tool = BlockingTool()
    provider = FakeProvider([mk_out(tc("c1", "block"))])
    cancel = asyncio.Event()
    driver, events = make_driver(tmp_path, provider, (tool,), cancel=cancel)
    task = asyncio.create_task(driver.run_turn(SessionState(), UserInput("go")))
    await asyncio.wait_for(tool.started.wait(), 1)
    cancel.set()
    report = await asyncio.wait_for(task, 1)
    assert report.result is not None and report.result.outcome == "aborted"
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished == [ToolFinished(ToolResult("c1", "cancelled", is_error=True))]
    turn_done = [e for e in events if isinstance(e, TurnFinished)]
    assert turn_done[-1].outcome == "aborted"
    assert_matched_pairs(report.state)


async def test_cancel_mid_ask_aborts_turn(tmp_path: Path) -> None:
    cancel = asyncio.Event()

    class HangingAsker:
        async def ask(self, question: PermissionQuestion) -> bool:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    log: list = []
    tools = (TimedTool("w1", Effects.WRITE_PATH, log),)
    provider = FakeProvider([mk_out(tc("c1", "w1"))])
    driver, events = make_driver(
        tmp_path, provider, tools, verdicts={"c1": "ask"}, asker=HangingAsker(), cancel=cancel
    )
    task = asyncio.create_task(driver.run_turn(SessionState(), UserInput("go")))
    await asyncio.sleep(0.02)
    cancel.set()
    report = await asyncio.wait_for(task, 1)
    assert report.result is not None and report.result.outcome == "aborted"
    decided = [e for e in events if isinstance(e, PermissionDecided)]
    assert decided[0].allowed is False and decided[0].reason == "cancelled"
    assert_matched_pairs(report.state)


async def test_retry_integration_logs_retry_scheduled(tmp_path: Path) -> None:
    events: list[Event] = []
    inner = FakeProvider([TransientProviderError("overloaded"), mk_out(TextBlock("ok"))])

    async def no_sleep(_: float) -> None:
        return None

    provider = RetryingProvider(
        inner, RetryConfig(), rng=lambda: 1.0, sleep=no_sleep, on_retry=events.append
    )
    driver = Driver(
        provider=provider,
        executor=ToolExecutor((), StubWorkspace(tmp_path)),
        asker=ScriptedAsker({}),
        policy=SimPolicy(),
        publish=events.append,
        cancel=asyncio.Event(),
    )
    report = await driver.run_turn(SessionState(), UserInput("go"))
    assert report.result is not None and report.result.outcome == "ok"
    retries = [e for e in events if isinstance(e, RetryScheduled)]
    assert len(retries) == 1 and "overloaded" in retries[0].reason
    assert len(inner.requests) == 2


async def test_fatal_provider_error_closes_turn_then_surfaces(tmp_path: Path) -> None:
    provider = FakeProvider([FatalProviderError("bad request")])
    driver, events = make_driver(tmp_path, provider)
    report = await driver.run_turn(SessionState(), UserInput("go"))
    assert isinstance(report.error, FatalProviderError)
    assert report.result is not None and report.result.outcome == "aborted"
    assert kinds(events)[-1] == "TurnFinished"  # log is well-formed despite the fault
    assert not report.state.in_turn
    assert_matched_pairs(report.state)


def make_standard_policy(tmp_path: Path, tools: tuple[ToolSpec, ...] = ()) -> StandardPolicy:
    return StandardPolicy(
        model="fake-model", context_tokens=200_000, tools=tools, ws_root=tmp_path
    )


async def test_usage_feedback_calibrates_policy_estimator(tmp_path: Path) -> None:
    policy = make_standard_policy(tmp_path)
    # The window is one 80-char user message: chars/4 estimates 20 tokens;
    # the scripted Usage reports 40 real input tokens, so the scale doubles.
    provider = FakeProvider([mk_out(TextBlock("hi"), usage=Usage(input_tokens=40))])
    driver = Driver(
        provider=provider,
        executor=ToolExecutor((), StubWorkspace(tmp_path)),
        asker=ScriptedAsker({}),
        policy=policy,
        publish=lambda e: None,
        cancel=asyncio.Event(),
    )
    assert policy.estimator.scale == 1.0
    report = await driver.run_turn(SessionState(), UserInput("x" * 80))
    assert report.result is not None and report.result.outcome == "ok"
    assert policy.estimator.scale == pytest.approx(2.0)


async def test_usage_feedback_lands_before_the_next_round(tmp_path: Path) -> None:
    log: list = []
    tool = TimedTool("peek", Effects.READ_PATH, log)
    policy = make_standard_policy(tmp_path, tools=(tool.spec,))
    scales: list[float] = []

    class SnoopingProvider(FakeProvider):
        async def complete(self, req, on_delta=None):
            scales.append(policy.estimator.scale)
            return await super().complete(req, on_delta)

    provider = SnoopingProvider(
        [
            mk_out(tc("c1", "peek"), usage=Usage(input_tokens=200)),
            mk_out(TextBlock("done"), usage=Usage(input_tokens=220)),
        ]
    )
    driver = Driver(
        provider=provider,
        executor=ToolExecutor((tool,), StubWorkspace(tmp_path)),
        asker=ScriptedAsker({}),
        policy=policy,
        publish=lambda e: None,
        cancel=asyncio.Event(),
    )
    report = await driver.run_turn(SessionState(), UserInput("go"))
    assert report.result is not None and report.result.outcome == "ok"
    # Round 1's Usage recalibrated the estimator before round 2's request
    # was built and sent — per-output feedback, not end-of-turn.
    assert scales[0] == 1.0
    assert scales[1] > 1.0


async def test_finished_state_ignores_further_input(tmp_path: Path) -> None:
    from dataclasses import replace

    provider = FakeProvider([])
    driver, events = make_driver(tmp_path, provider)
    finished = replace(SessionState(), finished=True)
    report = await driver.run_turn(finished, UserInput("hi"))
    assert report == TurnReport(state=finished, result=None, error=None)
    assert events == []
