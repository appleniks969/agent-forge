"""Tests for runner.drive — the shared agent_loop drain helper."""
from __future__ import annotations

import asyncio

import pytest

from agent_forge.loop import (
    AbortedAgentEvent, AgentEvent, AgentResult, DoneAgentEvent,
    TextDeltaAgentEvent, ToolResultAgentEvent,
    make_config,
)
from agent_forge.messages import UserMessage
from agent_forge.models import DEFAULT_MODEL
from agent_forge.runner import drive
from agent_forge.tools import default_registry

from .fake_provider import FakeProvider, text_turn, tool_turn


def _cfg(scripts, *, signal=None):
    return make_config(
        model=DEFAULT_MODEL, api_key=None, system_prompt=[],
        tool_registry=default_registry(), cwd=".",
        provider=FakeProvider(scripts), signal=signal,
    )


# ── Happy path: clean DoneAgentEvent ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_drive_returns_result_on_clean_finish():
    cfg = _cfg([text_turn("hello")])
    result = await drive(cfg, [UserMessage(content="hi")])
    assert isinstance(result, AgentResult)
    assert result.text == "hello"
    assert result.aborted is False


# ── on_event is called for every yielded event ───────────────────────────────

@pytest.mark.asyncio
async def test_drive_invokes_on_event_for_every_event():
    cfg = _cfg([text_turn("hi")])
    seen: list[type] = []
    result = await drive(
        cfg, [UserMessage(content="x")],
        on_event=lambda ev: seen.append(type(ev)),
    )
    assert result is not None
    # Must have seen at least the text delta and the done event
    assert TextDeltaAgentEvent in seen
    assert DoneAgentEvent in seen


@pytest.mark.asyncio
async def test_drive_works_without_on_event():
    cfg = _cfg([text_turn("hi")])
    result = await drive(cfg, [UserMessage(content="x")])
    assert result is not None and result.text == "hi"


# ── Abort path: signal set mid-stream → drive returns None ───────────────────

@pytest.mark.asyncio
async def test_drive_returns_none_when_aborted_mid_stream(tmp_path):
    """Set the abort signal before the run starts: agent_loop yields
    AbortedAgentEvent without DoneAgentEvent, drive should return None."""
    signal = asyncio.Event()
    signal.set()  # already set, so first turn aborts immediately
    cfg = _cfg([text_turn("never streamed")], signal=signal)
    result = await drive(cfg, [UserMessage(content="hi")])
    assert result is None


@pytest.mark.asyncio
async def test_drive_abort_event_is_still_passed_to_on_event():
    """on_event sees AbortedAgentEvent before drive returns None."""
    signal = asyncio.Event()
    signal.set()
    cfg = _cfg([text_turn("x")], signal=signal)
    seen: list[AgentEvent] = []
    result = await drive(
        cfg, [UserMessage(content="hi")],
        on_event=lambda ev: seen.append(ev),
    )
    assert result is None
    assert any(isinstance(ev, AbortedAgentEvent) for ev in seen)


# ── Tool-call run drains correctly via drive ─────────────────────────────────

@pytest.mark.asyncio
async def test_drive_handles_multi_turn_tool_call(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("contents")
    cfg = make_config(
        model=DEFAULT_MODEL, api_key=None, system_prompt=[],
        tool_registry=default_registry(), cwd=str(tmp_path),
        provider=FakeProvider([
            tool_turn("c1", "Read", {"path": "x.txt"}),
            text_turn("done"),
        ]),
    )
    seen_tool_results: list[ToolResultAgentEvent] = []
    result = await drive(
        cfg, [UserMessage(content="read")],
        on_event=lambda ev: (
            seen_tool_results.append(ev) if isinstance(ev, ToolResultAgentEvent) else None
        ),
    )
    assert result is not None
    assert result.text == "done"
    assert result.turns == 2
    assert len(seen_tool_results) == 1
    assert "contents" in seen_tool_results[0].result.content
