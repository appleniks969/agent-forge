"""Tests for AgentRuntime.run_turn() — the agent_loop drain seam.

Replaces the old tests/test_runner.py after runner.py was deleted (D.6).
The behaviours under test are equivalent — drain agent_loop into an
AgentResult, surface AbortedAgentEvent through on_event, return None on
abort — they now live inside AgentRuntime.run_turn() rather than in a
standalone drive() helper.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_forge.events import (
    AbortedAgentEvent, AgentEvent, DoneAgentEvent,
    TextDeltaAgentEvent, ToolResultAgentEvent,
)
from agent_forge.loop import AgentResult
from agent_forge.messages import UserMessage
from agent_forge.models import DEFAULT_MODEL
from agent_forge.runtime import AgentRuntime
from agent_forge.system_prompt import SystemPrompt
from agent_forge.tools import default_registry

from .fake_provider import FakeProvider, text_turn, tool_turn


def _runtime(scripts, *, cwd: str = ".") -> AgentRuntime:
    return AgentRuntime(
        model=DEFAULT_MODEL,
        system_prompt=SystemPrompt(),
        tool_registry=default_registry(),
        cwd=cwd,
        provider=FakeProvider(scripts),
    )


# ── Happy path: clean DoneAgentEvent ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_turn_returns_result_on_clean_finish():
    rt = _runtime([text_turn("hello")])
    result = await rt.run_turn(UserMessage(content="hi"))
    assert isinstance(result, AgentResult)
    assert result.text == "hello"
    assert result.aborted is False


# ── on_event called for every event ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_turn_invokes_on_event_for_every_event():
    rt = _runtime([text_turn("hi")])
    seen: list[type] = []
    result = await rt.run_turn(
        UserMessage(content="x"),
        on_event=lambda ev: seen.append(type(ev)),
    )
    assert result is not None
    assert TextDeltaAgentEvent in seen
    assert DoneAgentEvent in seen


@pytest.mark.asyncio
async def test_run_turn_works_without_on_event():
    rt = _runtime([text_turn("hi")])
    result = await rt.run_turn(UserMessage(content="x"))
    assert result is not None and result.text == "hi"


# ── Abort path ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_turn_returns_none_when_aborted_mid_stream():
    rt = _runtime([text_turn("never streamed")])
    signal = asyncio.Event()
    signal.set()
    result = await rt.run_turn(UserMessage(content="hi"), signal=signal)
    assert result is None


@pytest.mark.asyncio
async def test_run_turn_abort_event_still_passed_to_on_event():
    rt = _runtime([text_turn("x")])
    signal = asyncio.Event()
    signal.set()
    seen: list[AgentEvent] = []
    result = await rt.run_turn(
        UserMessage(content="hi"),
        signal=signal,
        on_event=lambda ev: seen.append(ev),
    )
    assert result is None
    assert any(isinstance(ev, AbortedAgentEvent) for ev in seen)


# ── Multi-turn with tool call ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_turn_handles_multi_turn_tool_call(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("contents")
    rt = AgentRuntime(
        model=DEFAULT_MODEL,
        system_prompt=SystemPrompt(),
        tool_registry=default_registry(),
        cwd=str(tmp_path),
        provider=FakeProvider([
            tool_turn("c1", "Read", {"path": "x.txt"}),
            text_turn("done"),
        ]),
    )
    seen_tool_results: list[ToolResultAgentEvent] = []
    result = await rt.run_turn(
        UserMessage(content="read"),
        on_event=lambda ev: (
            seen_tool_results.append(ev) if isinstance(ev, ToolResultAgentEvent) else None
        ),
    )
    assert result is not None
    assert result.text == "done"
    assert result.turns == 2
    assert len(seen_tool_results) == 1
    assert "contents" in seen_tool_results[0].result.content


# ── D.2 set_model + set_budget ───────────────────────────────────────────────

def test_runtime_set_model_propagates():
    """set_model rewires both the runtime and the context window's model."""
    from agent_forge.models import MODELS
    rt = _runtime([text_turn("hi")])
    other = next(m for m in MODELS.values() if m.id != rt.model.id)
    rt.set_model(other)
    assert rt.model.id == other.id
    assert rt.context._model.id == other.id


def test_runtime_set_budget_propagates():
    from agent_forge.context import ContextBudget
    rt = _runtime([text_turn("hi")])
    new = ContextBudget(
        keep_recent_tokens=99, recency_turns=2, p4_max_bytes=512, tool_max_bytes=1024,
    )
    rt.set_budget(new)
    assert rt.budget is new
    assert rt.context._budget is new


def test_runtime_make_cfg_threads_tool_max_bytes_from_budget():
    """make_cfg() should source tool_max_bytes from the budget, not from a
    hard-coded loop default. Single source of truth (D.3)."""
    from agent_forge.context import ContextBudget
    rt = _runtime([text_turn("hi")])
    rt.set_budget(ContextBudget(
        keep_recent_tokens=10_000, recency_turns=5,
        p4_max_bytes=4096, tool_max_bytes=12345,
    ))
    cfg = rt.make_cfg()
    assert cfg.tool_max_bytes == 12345


# ── AgentRuntime async context-manager lifecycle ─────────────────────────────
# Verifies:
#   • `async with AgentRuntime(...)` works (protocol implementation)
#   • `aclose()` is idempotent (safe to call twice or never)
#   • `aclose()` propagates to provider / tool_registry / hooks if they have one
#   • Cleanup order: hooks → tool_registry → provider
#   • A failing collaborator aclose() never blocks the others
#   • Exceptions inside the `async with` body still trigger cleanup

from agent_forge.hooks import NoopHooks
from agent_forge.models import MODELS
from agent_forge.tools import ToolRegistry


_MODEL_LIFECYCLE = MODELS["claude-sonnet-4-6"]


def _make_lifecycle_runtime(*, provider=None, tool_registry=None, hooks=None) -> AgentRuntime:
    return AgentRuntime(
        model=_MODEL_LIFECYCLE,
        system_prompt=SystemPrompt(),
        tool_registry=tool_registry or default_registry(),
        cwd=".",
        api_key="dummy",
        provider=provider,
        hooks=hooks,
    )


@pytest.mark.asyncio
async def test_aenter_returns_self():
    rt = _make_lifecycle_runtime()
    async with rt as entered:
        assert entered is rt


@pytest.mark.asyncio
async def test_aclose_is_idempotent():
    rt = _make_lifecycle_runtime()
    await rt.aclose()
    await rt.aclose()  # second call is a no-op, must not raise
    assert rt._closed


@pytest.mark.asyncio
async def test_aclose_without_enter_is_safe():
    """Runtime can be constructed and discarded without `async with`."""
    rt = _make_lifecycle_runtime()
    await rt.aclose()  # should not raise


@pytest.mark.asyncio
async def test_aexit_calls_aclose():
    rt = _make_lifecycle_runtime()
    async with rt:
        assert not rt._closed
    assert rt._closed


# ── Cleanup propagation to collaborators ─────────────────────────────────────

class _CountingCloser:
    """Records aclose() calls. Order-stamps via shared `order` list."""

    def __init__(self, name: str, order: list[str], *, raises: bool = False):
        self.name = name
        self.order = order
        self.raises = raises
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1
        self.order.append(self.name)
        if self.raises:
            raise RuntimeError(f"{self.name} cleanup blew up")


class _ProviderWithClose(FakeProvider):
    def __init__(self, name: str, order: list[str], *, raises: bool = False):
        super().__init__([])
        self._closer = _CountingCloser(name, order, raises=raises)

    async def aclose(self) -> None:
        await self._closer.aclose()


class _RegistryWithClose(ToolRegistry):
    def __init__(self, name: str, order: list[str], *, raises: bool = False):
        super().__init__()
        self._closer = _CountingCloser(name, order, raises=raises)

    async def aclose(self) -> None:
        await self._closer.aclose()


class _HooksWithClose(NoopHooks):
    def __init__(self, name: str, order: list[str], *, raises: bool = False):
        self._closer = _CountingCloser(name, order, raises=raises)

    async def aclose(self) -> None:
        await self._closer.aclose()


@pytest.mark.asyncio
async def test_aclose_propagates_to_provider():
    order: list[str] = []
    provider = _ProviderWithClose("provider", order)
    rt = _make_lifecycle_runtime(provider=provider)
    async with rt:
        pass
    assert provider._closer.closed == 1


@pytest.mark.asyncio
async def test_aclose_propagates_to_tool_registry():
    order: list[str] = []
    registry = _RegistryWithClose("registry", order)
    rt = _make_lifecycle_runtime(tool_registry=registry)
    async with rt:
        pass
    assert registry._closer.closed == 1


@pytest.mark.asyncio
async def test_aclose_propagates_to_hooks():
    order: list[str] = []
    hooks = _HooksWithClose("hooks", order)
    rt = _make_lifecycle_runtime(hooks=hooks)
    async with rt:
        pass
    assert hooks._closer.closed == 1


@pytest.mark.asyncio
async def test_aclose_order_is_hooks_then_registry_then_provider():
    """Tools may hold sockets (e.g. MCP) that need to drain before the
    HTTP provider closes. Hooks run first — they may want to write final
    audit logs before tools/provider tear down."""
    order: list[str] = []
    hooks = _HooksWithClose("hooks", order)
    registry = _RegistryWithClose("registry", order)
    provider = _ProviderWithClose("provider", order)
    rt = _make_lifecycle_runtime(provider=provider, tool_registry=registry, hooks=hooks)
    async with rt:
        pass
    assert order == ["hooks", "registry", "provider"]


@pytest.mark.asyncio
async def test_aclose_swallows_collaborator_errors():
    """One blowing-up aclose() must not prevent the others from running."""
    order: list[str] = []
    hooks = _HooksWithClose("hooks", order, raises=True)
    registry = _RegistryWithClose("registry", order)
    provider = _ProviderWithClose("provider", order, raises=True)
    rt = _make_lifecycle_runtime(provider=provider, tool_registry=registry, hooks=hooks)
    # Must not raise out of aclose
    await rt.aclose()
    # All three got called even though two raised
    assert hooks._closer.closed == 1
    assert registry._closer.closed == 1
    assert provider._closer.closed == 1
    assert order == ["hooks", "registry", "provider"]


# ── Exception safety ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exception_inside_async_with_still_cleans_up():
    order: list[str] = []
    provider = _ProviderWithClose("provider", order)
    rt = _make_lifecycle_runtime(provider=provider)
    with pytest.raises(ValueError, match="boom"):
        async with rt:
            raise ValueError("boom")
    assert rt._closed
    assert provider._closer.closed == 1


# ── Backward compat: collaborators without aclose() are fine ─────────────────

@pytest.mark.asyncio
async def test_aclose_skips_collaborators_without_aclose():
    """default_registry tools / NoopHooks / a plain FakeProvider have no
    aclose() — runtime.aclose() must just skip them silently."""
    rt = _make_lifecycle_runtime(
        provider=FakeProvider([]),         # no aclose
        tool_registry=default_registry(),  # built-in ToolRegistry has no aclose
        hooks=NoopHooks(),                 # no aclose
    )
    await rt.aclose()  # must not raise
    assert rt._closed


# ── CompactionAgentEvent wiring (manage_pressure → event) ────────────────────

from agent_forge.events import CompactionAgentEvent
from agent_forge.messages import AssistantMessage, TextContent, TokenUsage


@pytest.mark.asyncio
async def test_runtime_emits_compaction_event_under_pressure(monkeypatch):
    """When manage_pressure() returns non-NONE tier, run_turn emits CompactionAgentEvent."""
    from agent_forge.context import ContextWindow, PressureTier
    from agent_forge.provider import DoneEvent

    asst = AssistantMessage(
        content=(TextContent(text="ok"),),
        usage=TokenUsage(input=100, output=10, cache_read=0, cache_write=0),
    )
    provider = FakeProvider([[DoneEvent(message=asst)]])

    rt = AgentRuntime(
        model=DEFAULT_MODEL,
        system_prompt=SystemPrompt(),
        tool_registry=default_registry(),
        cwd=".",
        provider=provider,
    )

    # Force manage_pressure to report P3 (mid-pressure) so the event fires.
    async def fake_mp(self):
        return PressureTier.P3
    monkeypatch.setattr(ContextWindow, "manage_pressure", fake_mp)

    events: list = []
    result = await rt.run_turn(
        UserMessage(content="hi"),
        on_event=events.append,
    )
    assert result is not None
    compaction = [e for e in events if isinstance(e, CompactionAgentEvent)]
    assert len(compaction) == 1
    assert compaction[0].tokens_before >= 0
    assert compaction[0].tokens_after >= 0


@pytest.mark.asyncio
async def test_runtime_no_compaction_event_when_tier_none(monkeypatch):
    """When pressure tier is NONE, no CompactionAgentEvent is emitted."""
    from agent_forge.provider import DoneEvent

    asst = AssistantMessage(
        content=(TextContent(text="ok"),),
        usage=TokenUsage(input=10, output=5, cache_read=0, cache_write=0),
    )
    provider = FakeProvider([[DoneEvent(message=asst)]])

    rt = AgentRuntime(
        model=DEFAULT_MODEL,
        system_prompt=SystemPrompt(),
        tool_registry=default_registry(),
        cwd=".",
        provider=provider,
    )

    events: list = []
    await rt.run_turn(UserMessage(content="hi"), on_event=events.append)
    assert not [e for e in events if isinstance(e, CompactionAgentEvent)]
