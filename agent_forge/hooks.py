"""
hooks.py — Hooks Protocol, HookDecision, NoopHooks, and call helpers.

Depends only on messages. Pulled out of loop.py so any composition root or
plugin can subclass NoopHooks without importing the agent loop algorithm.

Hooks let composition roots inject behaviour at three points without taking
a dependency on chat / autonomous internals: gate dangerous tool calls,
redact secrets from results, observe LLM calls, etc.

All three methods return None for "no change". A non-None return:
  before_llm_call   → replacement messages list
  before_tool_call  → HookDecision (block=True, reason=…) to veto the call
  after_tool_call   → replacement ToolResult (e.g. for redaction)

AgentConfig.hooks defaults to NoopHooks; downstream code may replace it.

Owns: HookDecision, Hooks (Protocol), NoopHooks, _hook_before_llm,
      _hook_before_tool, _hook_after_tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .messages import Message, ToolCallContent, ToolResult


@dataclass(frozen=True)
class HookDecision:
    block: bool = False
    reason: str | None = None


@runtime_checkable
class Hooks(Protocol):
    async def before_llm_call(
        self, messages: list[Message], turn: int,
    ) -> list[Message] | None: ...

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None: ...

    async def after_tool_call(
        self, call: ToolCallContent, result: ToolResult, turn: int,
    ) -> ToolResult | None: ...


class NoopHooks:
    """Default hooks implementation — every method returns None (no change)."""

    async def before_llm_call(
        self, messages: list[Message], turn: int,
    ) -> list[Message] | None:
        return None

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        return None

    async def after_tool_call(
        self, call: ToolCallContent, result: ToolResult, turn: int,
    ) -> ToolResult | None:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
#
# Thin wrappers around Hooks Protocol calls. They normalise the None-return
# convention so the agent_loop body reads cleanly (no per-call None checks).

async def _hook_before_llm(hooks: Hooks, messages: list[Message], turn: int) -> list[Message]:
    result = await hooks.before_llm_call(messages, turn)
    return result if result is not None else messages

async def _hook_before_tool(hooks: Hooks, call: ToolCallContent, turn: int) -> HookDecision:
    decision = await hooks.before_tool_call(call, turn)
    return decision if decision is not None else HookDecision()

async def _hook_after_tool(
    hooks: Hooks, call: ToolCallContent, result: ToolResult, turn: int,
) -> ToolResult | None:
    return await hooks.after_tool_call(call, result, turn)
