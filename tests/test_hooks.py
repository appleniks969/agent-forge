"""Tests for the Hooks protocol, NoopHooks default, and BashGuardHook."""
from __future__ import annotations

import pytest

from agent_forge.autonomous import BashGuardHook
from agent_forge.loop import (
    HookDecision, Hooks, NoopHooks, agent_loop, make_config,
)
from agent_forge.provider import (
    DEFAULT_MODEL, ToolCallContent, ToolResult, UserMessage,
)
from agent_forge.tools import default_registry

from .fake_provider import FakeProvider, text_turn, tool_turn


# ── NoopHooks ────────────────────────────────────────────────────────────────

def test_noop_hooks_satisfies_protocol():
    assert isinstance(NoopHooks(), Hooks)


@pytest.mark.asyncio
async def test_noop_hooks_returns_none_everywhere():
    h = NoopHooks()
    call = ToolCallContent(id="1", name="Bash", arguments={"command": "echo hi"})
    assert await h.before_llm_call([], 1) is None
    assert await h.before_tool_call(call, 1) is None
    assert await h.after_tool_call(call, ToolResult(content="ok"), 1) is None


@pytest.mark.asyncio
async def test_make_config_defaults_to_noop_hooks():
    cfg = make_config(
        model=DEFAULT_MODEL, api_key=None, system_prompt=[],
        tool_registry=default_registry(), cwd=".",
        provider=FakeProvider([text_turn("hi")]),
    )
    assert isinstance(cfg.hooks, NoopHooks)


# ── BashGuardHook ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,should_block,reason_substr", [
    ("sudo rm /etc/passwd",                   True,  "sudo"),
    ("rm -rf /",                              True,  "system / home"),
    ("rm -rf ~",                              True,  "system / home"),
    ("git push origin main --force",          True,  "force-push"),
    ("git push -f origin main",               True,  "force-push"),
    ("git reset --hard origin/main",          True,  "hard reset"),
    (":(){ :|:& };:",                         True,  "fork bomb"),
    # benign commands pass
    ("ls -la",                                False, ""),
    ("rm -rf build/",                         False, ""),
    ("git push origin feature",               False, ""),
    ("git reset --hard HEAD~1",               False, ""),
    ("echo sudo",                             True,  "sudo"),  # word-boundary still matches
])
@pytest.mark.asyncio
async def test_bash_guard_patterns(cmd: str, should_block: bool, reason_substr: str):
    h = BashGuardHook()
    call = ToolCallContent(id="1", name="Bash", arguments={"command": cmd})
    decision = await h.before_tool_call(call, turn=1)
    if should_block:
        assert decision is not None
        assert decision.block is True
        assert reason_substr.lower() in (decision.reason or "").lower()
    else:
        assert decision is None


@pytest.mark.asyncio
async def test_bash_guard_ignores_other_tools():
    h = BashGuardHook()
    call = ToolCallContent(id="1", name="Read", arguments={"path": "x"})
    assert await h.before_tool_call(call, turn=1) is None


# ── Wiring: blocking surfaces as ToolBlockedAgentEvent ───────────────────────

class _BlockBashHook(NoopHooks):
    async def before_tool_call(self, call, turn):
        if call.name == "Bash":
            return HookDecision(block=True, reason="test-block")
        return None


@pytest.mark.asyncio
async def test_blocked_tool_call_yields_blocked_event_and_continues():
    from agent_forge.loop import ToolBlockedAgentEvent, DoneAgentEvent

    fake = FakeProvider([
        tool_turn("c1", "Bash", {"command": "ls"}),
        text_turn("done"),
    ])
    cfg = make_config(
        model=DEFAULT_MODEL, api_key=None, system_prompt=[],
        tool_registry=default_registry(), cwd=".",
        provider=fake, hooks=_BlockBashHook(),
    )

    blocked: list[ToolBlockedAgentEvent] = []
    done = None
    async for ev in agent_loop(cfg, [UserMessage(content="run ls")]):
        if isinstance(ev, ToolBlockedAgentEvent):
            blocked.append(ev)
        if isinstance(ev, DoneAgentEvent):
            done = ev

    assert len(blocked) == 1
    assert blocked[0].name == "Bash"
    assert blocked[0].reason == "test-block"
    assert done is not None and done.result.text == "done"
