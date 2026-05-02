"""Smoke tests for agent_loop using the FakeProvider."""
from __future__ import annotations

import pytest

from agent_forge.loop import (
    AgentResult, DoneAgentEvent, TextDeltaAgentEvent, ToolResultAgentEvent,
    agent_loop, make_config, run_agent,
)
from agent_forge.provider import DEFAULT_MODEL, UserMessage
from agent_forge.tools import default_registry

from .fake_provider import FakeProvider, text_turn, tool_turn


@pytest.mark.asyncio
async def test_text_only_turn_yields_done_with_text():
    fake = FakeProvider([text_turn("hello world")])
    cfg = make_config(
        model=DEFAULT_MODEL,
        api_key=None,
        system_prompt=[],
        tool_registry=default_registry(),
        cwd=".",
        provider=fake,
    )
    events = []
    async for ev in agent_loop(cfg, [UserMessage(content="hi")]):
        events.append(ev)

    # Saw text deltas
    deltas = [e for e in events if isinstance(e, TextDeltaAgentEvent)]
    assert deltas and "".join(e.delta for e in deltas) == "hello world"

    # Terminated cleanly with the right text
    done = [e for e in events if isinstance(e, DoneAgentEvent)]
    assert len(done) == 1
    assert isinstance(done[0].result, AgentResult)
    assert done[0].result.text == "hello world"
    assert done[0].result.aborted is False
    assert done[0].result.turns == 1

    # Provider was called once with our user message
    assert len(fake.calls) == 1
    msgs = fake.calls[0].messages
    assert any(isinstance(m, UserMessage) and m.content == "hi" for m in msgs)


@pytest.mark.asyncio
async def test_tool_call_then_text_drains_two_turns(tmp_path):
    # Turn 1: model asks to read a file → tool runs → result fed back
    # Turn 2: model emits final text and stops
    target = tmp_path / "hello.txt"
    target.write_text("file contents", encoding="utf-8")

    fake = FakeProvider([
        tool_turn("call-1", "Read", {"path": "hello.txt"}),
        text_turn("done reading"),
    ])
    cfg = make_config(
        model=DEFAULT_MODEL,
        api_key=None,
        system_prompt=[],
        tool_registry=default_registry(),
        cwd=str(tmp_path),
        provider=fake,
    )
    result = await run_agent(cfg, [UserMessage(content="read it")])

    assert result.text == "done reading"
    assert result.turns == 2
    assert len(result.tool_calls) == 1
    rec = result.tool_calls[0]
    assert rec.name == "Read"
    assert "file contents" in rec.result.content
    assert rec.result.is_error is False


@pytest.mark.asyncio
async def test_run_agent_records_tool_event():
    fake = FakeProvider([text_turn("ok")])
    cfg = make_config(
        model=DEFAULT_MODEL, api_key=None, system_prompt=[],
        tool_registry=default_registry(), cwd=".", provider=fake,
    )
    seen: list[type] = []

    async def on_event(ev):
        seen.append(type(ev))

    result = await run_agent(cfg, [UserMessage(content="hi")], on_event=on_event)
    assert result.text == "ok"
    assert DoneAgentEvent in seen
