"""
runtime.py — AgentRuntime: per-session glue that pairs a ContextWindow with
an AgentConfig factory and the runner.drive() seam.

Depends on context, loop, runner, system_prompt, messages, models, provider,
tools. Sits one notch below the composition roots (chat.py, autonomous.py)
and removes the duplicated per-turn dance that used to live in both:

    initial = ctx.build_messages(user_msg)
    cfg     = make_config(...)            # built fresh because `signal` rotates
    result  = await drive(cfg, initial, on_event=...)
    ctx.sync_total_tokens(real_total)
    ctx.receive(user_msg, assistant_messages, tool_calls)
    ctx.sync_total_tokens(real_total)
    await ctx.manage_pressure()

Both REPL turns and autonomous phases now share the same dance, which means
autonomous.py inherits context-pressure management for free (RC2 fix).

Owns: AgentRuntime (constructor + run_turn() + clear() + init_messages()).
Does NOT own: session JSONL persistence (chat.py still writes JSONL after
              drive returns), KeyboardInterrupt handling (REPL-specific —
              chat.py wraps run_turn in try/except), worktree lifecycle
              (autonomous.py wraps run_turn in try/finally).

The runtime API is deliberately narrow — three methods. Anything richer
belongs in the composition root, not here. ADR-001 documents this.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from .context import ContextWindow, PressureTier
from .loop import (
    AgentConfig, AgentEvent, AgentResult, Hooks, make_config,
)
from .messages import Message, UserMessage
from .models import Model
from .provider import LLMProvider
from .runner import drive
from .system_prompt import SystemPrompt
from .tools import ToolRegistry


@dataclass
class AgentRuntime:
    """
    Owns a ContextWindow and constructs AgentConfigs on demand.

    Construct once per logical session (REPL session, autonomous phase),
    then call run_turn() per user message.
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

    _ctx: ContextWindow = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._ctx = ContextWindow(model=self.model)

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

    # ── Config factory ───────────────────────────────────────────────────────

    def make_cfg(self, *, signal: asyncio.Event | None = None) -> AgentConfig:
        """Build a fresh AgentConfig — the abort signal rotates per turn."""
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
        result = await drive(cfg, initial, on_event=on_event)
        if result is None:
            return None

        # Real API total is (input + cache_read). Sync it before receive() so
        # estimate_tokens() is accurate if the caller peeks; receive() will
        # reset it (new messages added), so we sync again afterwards.
        api_total = result.usage.input + result.usage.cache_read
        self._ctx.sync_total_tokens(api_total)
        self._ctx.receive(
            user_message=user_message,
            assistant_messages=[m for m in result.messages if not isinstance(m, UserMessage)],
            tool_calls=result.tool_calls,
        )
        self._ctx.sync_total_tokens(api_total)
        await self._ctx.manage_pressure()
        return result

    # ── Diagnostic ───────────────────────────────────────────────────────────

    def pressure_tier(self) -> PressureTier:
        return self._ctx.pressure_tier()
