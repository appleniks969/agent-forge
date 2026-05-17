"""
hooks.py — Hooks Protocol, HookDecision, NoopHooks, and call helpers.

Depends only on messages. Pulled out of loop.py so any composition root or
plugin can subclass NoopHooks without importing the agent loop algorithm.

Hooks let composition roots inject behaviour at three points without taking
a dependency on chat internals: gate dangerous tool calls, redact secrets
from results, observe LLM calls, etc. The reusable guard implementations
(BashGuardHook, PathGuardHook, MCPGuardHook, _CompositeHook) live in
guards.py.

All three methods return None for "no change". A non-None return:
  before_llm_call   → replacement messages list
  before_tool_call  → HookDecision (block=True, reason=…) to veto the call
  after_tool_call   → replacement ToolResult (e.g. for redaction)

AgentConfig.hooks defaults to NoopHooks; downstream code may replace it.

Persistence semantics (important — frequently misunderstood):

  before_llm_call return value is **transient**. The returned list is sent
  to the provider for THIS turn's API call only. The conversation history
  (ContextWindow + session JSONL) is NOT mutated — it still holds the
  original `messages`. This is the right shape for:
    - Wire-time secret redaction (don't ship the secret to the provider,
      but keep the original on disk in case the user needs to retrieve it)
    - Per-turn injection of stale-cache-busters

  It is the WRONG shape for "redact secrets out of the session JSONL too".
  For that, redact at the write-time seam in session.append_message()
  (Phase E) — the Hooks Protocol intentionally cannot mutate persisted
  history, because doing so would make the JSONL diverge silently from
  what the model actually saw.

  before_tool_call's HookDecision is also transient — the deny reason is
  surfaced via ToolBlockedAgentEvent and a synthesised ToolResult, but the
  original ToolCallContent block is preserved in the assistant message.

  after_tool_call's replacement ToolResult IS persisted (it becomes the
  ToolResultMessage in conversation). Use this for redaction that you DO
  want on disk.

Owns: HookDecision, Hooks (Protocol), NoopHooks, _hook_before_llm,
      _hook_before_tool, _hook_after_tool.
"""
from __future__ import annotations

import logging
import time
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


# ── AuditHook ─────────────────────────────────────────────────────────────────
#
# Default observability shape. Logs every tool call (before/after) at a single
# log level using the stdlib `logging` module. Cooperates with any
# `logging.basicConfig` / handlers the embedding application has set up; emits
# nothing if no handler is attached (Python's logging default).
#
# Intentionally minimal — it's a canonical example of a `NoopHooks` subclass
# more than a feature-rich audit framework. For richer observability
# (per-turn JSON lines, tracing, span correlation) compose your own hook by
# subclassing the same way; see docs/AGENTS.md "Worked Examples".


class AuditHook(NoopHooks):
    """Emit a structured log line per tool call.

    Three log records per tool call:

    - ``before_tool_call`` → ``"[audit] turn=N tool=NAME args=…"``
    - ``after_tool_call``  → ``"[audit] turn=N tool=NAME ok|error duration_ms=…"``
    - on block (via the standard hook chain — composes with guard hooks)

    Parameters
    ----------
    logger
        Optional ``logging.Logger`` to emit on. Defaults to
        ``logging.getLogger("agent_forge.audit")``.
    level
        Log level (default ``logging.INFO``).
    redact_args
        If True (default), tool arguments are summarised as
        ``{keys}`` only, never their values. Set ``False`` to log full
        argument dicts — only safe for trusted local environments.
    include_result_preview
        If True, also log the first 200 chars of every tool result.
        Off by default — results may contain user data.

    Example
    -------
    >>> import logging
    >>> logging.basicConfig(level=logging.INFO)
    >>> from agent_forge import make_config, AuditHook
    >>> cfg = make_config(..., hooks=AuditHook())
    """

    _MAX_PREVIEW = 200

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        level: int = logging.INFO,
        redact_args: bool = True,
        include_result_preview: bool = False,
    ) -> None:
        self._log = logger or logging.getLogger("agent_forge.audit")
        self._level = level
        self._redact_args = redact_args
        self._include_preview = include_result_preview
        # Maps tool_use_id → monotonic start time, set in before_tool_call.
        self._started: dict[str, float] = {}

    def _arg_summary(self, args: dict) -> str:
        if not self._redact_args:
            return repr(args)
        if not args:
            return "{}"
        return "{" + ",".join(sorted(args.keys())) + "}"

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        self._started[call.id] = time.monotonic()
        self._log.log(
            self._level,
            "[audit] turn=%d tool=%s args=%s id=%s",
            turn, call.name, self._arg_summary(call.arguments or {}), call.id,
        )
        return None

    async def after_tool_call(
        self, call: ToolCallContent, result: ToolResult, turn: int,
    ) -> ToolResult | None:
        start = self._started.pop(call.id, None)
        dur_ms = int((time.monotonic() - start) * 1000) if start is not None else -1
        status = "error" if result.is_error else "ok"
        if self._include_preview:
            preview = (result.content or "")[: self._MAX_PREVIEW]
            self._log.log(
                self._level,
                "[audit] turn=%d tool=%s %s duration_ms=%d id=%s preview=%r",
                turn, call.name, status, dur_ms, call.id, preview,
            )
        else:
            self._log.log(
                self._level,
                "[audit] turn=%d tool=%s %s duration_ms=%d id=%s",
                turn, call.name, status, dur_ms, call.id,
            )
        return None
