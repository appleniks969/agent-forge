"""
runtime.py — AgentRuntime: per-session glue that pairs a ContextWindow with
an AgentConfig factory and drains agent_loop into AgentResult.

Depends on context, loop, system_prompt, messages, models, provider, tools.
Sits one notch below the composition root (chat.py) and centralises the
per-turn dance:

    initial = ctx.build_messages(user_msg)
    cfg     = make_config(...)            # built fresh because `signal` rotates
    result  = drain agent_loop(cfg, initial)
    ctx.receive(user_msg, assistant_messages, tool_calls)
    ctx.sync_total_tokens(real_total)
    await ctx.manage_pressure()  → may emit CompactionAgentEvent

Owns: AgentRuntime (constructor + run_turn() + clear() + init_messages()
      + set_model() + set_budget() + aclose() + async-context-manager protocol).
Does NOT own: session JSONL persistence (chat.py still writes JSONL after
              run_turn returns), KeyboardInterrupt handling (REPL-specific —
              chat.py wraps run_turn in try/except).

Lifecycle: AgentRuntime supports `async with` so callers always get
deterministic cleanup of provider / tool-registry / hook / mcp_manager
resources. Order is hooks → mcp_manager → tool_registry → provider, chosen
so each layer's dependents have already drained when it tears down.
`aclose()` is idempotent and never raises — same policy as the loop's
"never raise out of cleanup" rule. Phase H's `build_runtime_with_mcp`
factory is responsible for calling `await mcp_manager.connect_all()` before
the first turn; AgentRuntime owns only the teardown half.

The runtime API is deliberately narrow. Anything richer belongs in the
composition root, not here. ADR-001 documents this.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from .context import ContextBudget, ContextWindow, PressureTier, default_budget
from .events import CompactionAgentEvent, DoneAgentEvent
from .loop import (
    AgentConfig, AgentEvent, AgentResult, Hooks, agent_loop, make_config,
)
from .messages import Message, UserMessage
from .models import Model
from .provider import LLMProvider
from .system_prompt import SystemPrompt
from .tools import ToolRegistry


@dataclass
class AgentRuntime:
    """
    Owns a ContextWindow and constructs AgentConfigs on demand.

    Construct once per logical session (REPL session), then call run_turn()
    per user message.
    """

    model: Model
    system_prompt: SystemPrompt
    tool_registry: ToolRegistry
    cwd: str
    api_key: str | None = None
    thinking: str = "medium"
    max_turns: int = 100
    max_tokens: int | None = None
    project_root: str | None = None
    provider: LLMProvider | None = None
    hooks: Hooks | None = None
    budget: ContextBudget | None = None
    # MCP integration — Phase G. The composition root (Phase H factory) is
    # responsible for `await mcp_manager.connect_all()` before the first
    # `run_turn`. AgentRuntime owns only the teardown half: aclose() shuts
    # the manager down as part of the standard cleanup chain.
    mcp_manager: object | None = None

    _ctx: ContextWindow = field(init=False, repr=False)
    _closed: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if self.budget is None:
            self.budget = default_budget(self.model)
        self._ctx = ContextWindow(model=self.model, budget=self.budget)

    # ── Window access ────────────────────────────────────────────────────────

    @property
    def context(self) -> ContextWindow:
        return self._ctx

    def init_messages(self, messages: list[Message]) -> None:
        """Seed the ContextWindow from a resumed session."""
        self._ctx.init_from_existing(messages)

    def clear(self) -> None:
        """Wipe the window and invalidate every cached system-prompt section."""
        self._ctx.clear()
        self.system_prompt.invalidate_all()

    def set_model(self, model: Model) -> None:
        """Switch model mid-session. Propagates to ContextWindow and invalidates
        the cached system prompt (cache hints depend on model identity)."""
        self.model = model
        self._ctx.set_model(model)
        self.system_prompt.invalidate_all()

    def set_budget(self, budget: ContextBudget) -> None:
        """Switch eviction thresholds mid-session. Takes effect on the next
        manage_pressure() / make_cfg() call."""
        self.budget = budget
        self._ctx.set_budget(budget)

    # ── Config factory ───────────────────────────────────────────────────────

    def make_cfg(self, *, signal: asyncio.Event | None = None) -> AgentConfig:
        """Build a fresh AgentConfig — the abort signal rotates per turn.

        tool_max_bytes is sourced from the ContextBudget so a single config
        value drives both loop-time truncation and context-eviction policy.
        """
        return make_config(
            model=self.model,
            api_key=self.api_key,
            system_prompt=self.system_prompt.build(),
            tool_registry=self.tool_registry,
            cwd=self.cwd,
            thinking=self.thinking,
            max_turns=self.max_turns,
            max_tokens=self.max_tokens if self.max_tokens is not None else self.model.max_tokens,
            project_root=self.project_root,
            signal=signal,
            hooks=self.hooks,
            provider=self.provider,
            tool_max_bytes=self.budget.tool_max_bytes,
        )

    # ── Per-turn ─────────────────────────────────────────────────────────────

    async def run_turn(
        self,
        user_message: UserMessage,
        *,
        signal: asyncio.Event | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentResult | None:
        """
        Run one full agent turn. Returns the AgentResult, or None on abort
        (signal fired before DoneAgentEvent — runner.drive() returned None).

        On success, advances the ContextWindow and runs pressure management.
        Persistence is the caller's job (we don't write JSONL here).
        """
        initial = self._ctx.build_messages(user_message)
        cfg = self.make_cfg(signal=signal)

        # Drain agent_loop in-line — the old runner.drive() seam was a 6-line
        # function with one caller (this one). Keeping the loop here removes
        # a layer that had no substitution value (no second consumer, no
        # alternative implementation).
        result: AgentResult | None = None
        async for ev in agent_loop(cfg, initial):
            if on_event is not None:
                on_event(ev)
            if isinstance(ev, DoneAgentEvent):
                result = ev.result
        if result is None:
            return None

        # Real API total is (input + cache_read). receive() resets the synced
        # total because new messages have just been appended, so we sync AFTER
        # receive() to give estimate_tokens() the authoritative count to fall
        # back on (heuristic estimate of unseen blocks added on top).
        api_total = result.usage.input + result.usage.cache_read
        self._ctx.receive(
            user_message=user_message,
            assistant_messages=[m for m in result.messages if not isinstance(m, UserMessage)],
            tool_calls=result.tool_calls,
        )
        self._ctx.sync_total_tokens(api_total)

        tokens_before = self._ctx.estimate_tokens()
        tier = await self._ctx.manage_pressure()
        if tier is not PressureTier.NONE and on_event is not None:
            on_event(CompactionAgentEvent(
                tokens_before=tokens_before,
                tokens_after=self._ctx.estimate_tokens(),
            ))
        return result

    # ── Diagnostic ───────────────────────────────────────────────────────────

    def pressure_tier(self) -> PressureTier:
        return self._ctx.pressure_tier()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release async resources owned by collaborators (provider, tool
        registry, hooks). Idempotent; safe to call without ever entering as
        a context manager. Best-effort: a failing aclose() on one
        collaborator never blocks the others.

        This is the seam MCP (Phase G) plugs into — `ToolRegistry` will own
        the MCP client sessions and expose its own `aclose()`.
        """
        if self._closed:
            return
        self._closed = True
        # Order matters: hooks first (may write audit logs and want a live
        # registry/provider when they do), then mcp_manager (subprocess
        # servers must drain before their dependents), then tool_registry
        # (any non-MCP tool that holds sockets), then provider (HTTP client
        # last so retries during teardown still work).
        for obj in (self.hooks, self.mcp_manager, self.tool_registry, self.provider):
            close = getattr(obj, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                # Cleanup must never raise — mirror the loop's policy.
                pass

    async def __aenter__(self) -> AgentRuntime:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


# ── Composition factory: build_runtime_with_mcp ──────────────────────────────

async def build_runtime_with_mcp(
    *,
    model: Model,
    system_prompt: SystemPrompt,
    tool_registry: ToolRegistry,
    cwd: str,
    mcp_configs: list,                       # list[MCPServerConfig]; loose-typed to avoid forward ref
    api_key: str | None = None,
    thinking: str = "medium",
    max_turns: int = 100,
    max_tokens: int | None = None,
    project_root: str | None = None,
    provider: LLMProvider | None = None,
    hooks: Hooks | None = None,
    budget: ContextBudget | None = None,
    on_status: Callable[[str, str, str | None], None] | None = None,
) -> AgentRuntime:
    """Build an ``AgentRuntime`` with MCP servers connected and tools live.

    Sequence:
      1. ``MCPManager(mcp_configs)`` (skipped when ``mcp_configs`` is empty;
         resulting runtime has ``mcp_manager=None``)
      2. ``await mgr.connect_all()``
      3. ``tool_registry.replace_mcp_tools(mgr.tools())``
      4. ``AgentRuntime(..., mcp_manager=mgr)``

    The runtime's ``aclose()`` chain shuts the manager down — callers don't
    need to remember a separate teardown. Use as::

        async with await build_runtime_with_mcp(...) as runtime:
            await runtime.run_turn(...)

    ``on_status(server_name, status_value, error_or_none)`` is called once
    per server after ``connect_all`` so the REPL can print a one-line
    summary without this factory printing anything itself (keeps it pure).
    """
    from .mcp import MCPManager   # local import — avoid a top-level cycle with __init__

    mgr: MCPManager | None = None
    if mcp_configs:
        mgr = MCPManager(mcp_configs)
        await mgr.connect_all()
        tool_registry.replace_mcp_tools(mgr.tools())
        if on_status is not None:
            for c in mgr.clients:
                on_status(c.config.name, c.status.value, c.error)

    return AgentRuntime(
        model=model,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        cwd=cwd,
        api_key=api_key,
        thinking=thinking,
        max_turns=max_turns,
        max_tokens=max_tokens,
        project_root=project_root,
        provider=provider,
        hooks=hooks,
        budget=budget,
        mcp_manager=mgr,
    )
