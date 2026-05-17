"""Tests for the Hooks protocol, NoopHooks default, and BashGuardHook."""
# Note: BashGuardHook lives in agent_forge.guards (since the autonomous-mode
# removal). The hook is still a generic safety policy applicable to any
# composition root.
from __future__ import annotations

import pytest

from agent_forge.guards import BashGuardHook
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


# ── _CompositeHook ───────────────────────────────────────────────────────────
# Composite-hook contract: before_llm fan-out, before_tool first-veto-wins
# with full audit fan-out, after_tool result chaining.

from agent_forge.guards import _CompositeHook


class _NoopRecording(NoopHooks):
    def __init__(self) -> None:
        self.llm_calls = 0
        self.tool_calls = 0
        self.after_calls = 0

    async def before_llm_call(self, messages, turn):
        self.llm_calls += 1
        return None

    async def before_tool_call(self, call, turn):
        self.tool_calls += 1
        return None

    async def after_tool_call(self, call, result, turn):
        self.after_calls += 1
        return None


@pytest.mark.asyncio
async def test_composite_before_llm_returns_none_when_unchanged():
    h1, h2 = _NoopRecording(), _NoopRecording()
    composite = _CompositeHook(h1, h2)
    result = await composite.before_llm_call([UserMessage(content="hi")], 0)
    assert result is None
    assert h1.llm_calls == 1 and h2.llm_calls == 1


@pytest.mark.asyncio
async def test_composite_before_llm_returns_messages_when_mutated():
    class _MutateHook(NoopHooks):
        async def before_llm_call(self, messages, turn):
            return [UserMessage(content="mutated")]

    composite = _CompositeHook(_NoopRecording(), _MutateHook(), _NoopRecording())
    result = await composite.before_llm_call([UserMessage(content="orig")], 0)
    assert result is not None
    assert result[0].content == "mutated"


class _DenyHook(NoopHooks):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def before_tool_call(self, call, turn):
        return HookDecision(block=True, reason=self.reason)


@pytest.mark.asyncio
async def test_composite_before_tool_runs_all_audit_after_veto():
    """An audit hook installed AFTER a deny hook still sees the call."""
    audit = _NoopRecording()
    composite = _CompositeHook(_DenyHook("nope"), audit)
    call = ToolCallContent(id="x", name="Bash", arguments={})

    decision = await composite.before_tool_call(call, 0)
    assert decision is not None and decision.block is True
    assert decision.reason == "nope"
    # The key fix: audit hook ran despite the earlier deny.
    assert audit.tool_calls == 1


@pytest.mark.asyncio
async def test_composite_before_tool_first_deny_wins():
    """When multiple hooks deny, the first one's reason is returned."""
    composite = _CompositeHook(_DenyHook("first"), _DenyHook("second"))
    call = ToolCallContent(id="x", name="Bash", arguments={})

    decision = await composite.before_tool_call(call, 0)
    assert decision.block is True
    assert decision.reason == "first"


@pytest.mark.asyncio
async def test_composite_before_tool_returns_none_when_no_deny():
    composite = _CompositeHook(_NoopRecording(), _NoopRecording())
    call = ToolCallContent(id="x", name="Bash", arguments={})

    decision = await composite.before_tool_call(call, 0)
    assert decision is None


@pytest.mark.asyncio
async def test_composite_after_tool_chains_rewrites():
    class _AppendHook(NoopHooks):
        def __init__(self, tag: str) -> None:
            self.tag = tag

        async def after_tool_call(self, call, result, turn):
            return ToolResult(content=result.content + self.tag, is_error=result.is_error)

    composite = _CompositeHook(_AppendHook("-a"), _AppendHook("-b"))
    call = ToolCallContent(id="x", name="Bash", arguments={})
    result = await composite.after_tool_call(call, ToolResult(content="x"), 0)
    assert result.content == "x-a-b"


@pytest.mark.asyncio
async def test_composite_after_tool_returns_none_when_unchanged():
    composite = _CompositeHook(_NoopRecording(), _NoopRecording())
    call = ToolCallContent(id="x", name="Bash", arguments={})
    result = await composite.after_tool_call(call, ToolResult(content="x"), 0)
    assert result is None


# ── hooks module docstring documents persistence semantics ───────────────────

def test_hooks_module_docstring_documents_persistence():
    import agent_forge.hooks as hooks_mod
    doc = hooks_mod.__doc__
    assert doc is not None
    assert "transient" in doc.lower()
    assert "session" in doc.lower() or "jsonl" in doc.lower()
    assert "persist" in doc.lower()


# ── PathGuardHook ────────────────────────────────────────────────────────────

from agent_forge.guards import PathGuardHook


@pytest.mark.parametrize("path,blocked", [
    ("/etc/passwd",            True),
    ("/usr/local/bin/x",       True),
    ("~/.ssh/id_rsa",          True),
    ("~/.aws/credentials",     True),
    ("src/main.py",            False),
    ("/tmp/foo.txt",           False),
])
@pytest.mark.asyncio
async def test_path_guard_blocks_sensitive_writes(path, blocked):
    h = PathGuardHook()
    call = ToolCallContent(id="1", name="Write", arguments={"path": path, "content": ""})
    decision = await h.before_tool_call(call, turn=1)
    if blocked:
        assert decision is not None and decision.block is True
    else:
        assert decision is None


@pytest.mark.asyncio
async def test_path_guard_ignores_non_write_tools():
    h = PathGuardHook()
    call = ToolCallContent(id="1", name="Read", arguments={"path": "/etc/passwd"})
    assert await h.before_tool_call(call, turn=1) is None


@pytest.mark.asyncio
async def test_path_guard_custom_deny_list():
    h = PathGuardHook(deny_paths=("/sensitive",))
    call = ToolCallContent(id="1", name="Write", arguments={"path": "/sensitive/x"})
    d = await h.before_tool_call(call, turn=1)
    assert d is not None and d.block is True
    # Default-blocked path is NOT in the custom list — should pass
    call2 = ToolCallContent(id="2", name="Write", arguments={"path": "/etc/x"})
    assert await h.before_tool_call(call2, turn=1) is None


# ── _CompositeHook integration: BashGuard + PathGuard chained ────────────────

@pytest.mark.asyncio
async def test_composite_hook_first_veto_wins():
    chained = _CompositeHook(BashGuardHook(), PathGuardHook())
    bash_call = ToolCallContent(id="1", name="Bash", arguments={"command": "sudo ls"})
    d = await chained.before_tool_call(bash_call, turn=1)
    assert d is not None and d.block is True and "sudo" in (d.reason or "").lower()

    write_call = ToolCallContent(id="2", name="Write", arguments={"path": "/etc/x", "content": ""})
    d2 = await chained.before_tool_call(write_call, turn=1)
    assert d2 is not None and d2.block is True

    benign = ToolCallContent(id="3", name="Bash", arguments={"command": "ls"})
    assert await chained.before_tool_call(benign, turn=1) is None
