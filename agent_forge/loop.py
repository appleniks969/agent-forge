"""
loop.py — agent_loop() async generator + AgentEvent/AgentConfig/AgentResult types.

Depends on provider, tools, and context. Orchestrates one full task: context
builds the message array → loop calls the LLM via provider → tools execute tool
calls → loop appends results back to the message list. Session persistence is
deliberately excluded (that belongs to chat.py) so the loop stays a pure
generator that can be tested without disk I/O.

Owns: agent_loop() (the core async generator), all AgentEvent frozen dataclasses,
      AgentConfig, AgentResult, make_config() (factory that injects cwd into the
      tool registry), run_agent() (convenience drain), _CwdPatchedRegistry /
      _CwdBoundTool, retry logic (_retry_delay), tool result truncation
      (_truncate_tool_result), Hooks Protocol + NoopHooks default + HookDecision.

AgentEvent types (16):
  TurnStartEvent, TurnEndEvent
  ThinkingStartAgentEvent, ThinkingDeltaAgentEvent, ThinkingEndAgentEvent
  TextStartAgentEvent, TextDeltaAgentEvent, TextEndAgentEvent
  ToolDeclaredAgentEvent   — LLM committed to a tool call (from stream, before exec)
  ToolExecutingAgentEvent  — about to call tool.execute()
  ToolResultAgentEvent     — tool returned
  ToolBlockedAgentEvent    — hook blocked the call
  ErrorAgentEvent, AbortedAgentEvent, CompactionAgentEvent, DoneAgentEvent

Policies enforced here:
  - Turn completeness: partial assistant messages are never appended on error/abort.
  - Abort completeness: remaining unexecuted tool calls get placeholder error results
    before AbortedAgentEvent so the API never sees an unmatched tool_use block.
  - Tool result truncation: results > 50 KB truncated before appending to context.
  - Retry: exponential backoff + jitter, up to 3 attempts per turn, max 30 s.
  - Max-turns exits via DoneAgentEvent(result.aborted=True) — single exit path for callers.
  - api_key is encapsulated in AnthropicProvider; AgentConfig.provider carries it.
    make_config() also accepts a pre-built provider= kwarg for testing.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .anthropic_provider import AnthropicProvider
from .messages import (
    AssistantMessage, Message, SystemPromptSection, TextContent, ThinkingContent,
    ToolCallContent, ToolDefinition, ToolResult, ToolResultMessage, TokenUsage,
    UserMessage, ZERO_USAGE,
)
from .models import Model
from .provider import (
    ContentBlockEndEvent, ContentBlockStartEvent, DoneEvent, StreamErrorEvent,
    TextDeltaEvent, ThinkingDeltaEvent, ToolCallEndEvent,
)
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0

def _retry_delay(attempt: int) -> float:
    return min(_BASE_DELAY * (2 ** attempt) * random.uniform(0.75, 1.25), _MAX_DELAY)

# ── Hooks Protocol ────────────────────────────────────────────────────────────
#
# Hooks let composition roots inject behaviour at three points without taking
# a dependency on chat / autonomous internals: gate dangerous tool calls,
# redact secrets from results, observe LLM calls, etc.
#
# All three methods return None for "no change". A non-None return:
#   before_llm_call → replacement messages list
#   before_tool_call → HookDecision (block=True, reason=…) to veto the call
#   after_tool_call → replacement ToolResult (e.g. for redaction)
#
# AgentConfig.hooks defaults to NoopHooks; downstream code may replace it.

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


# ── Agent event types ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TurnStartEvent:
    turn: int
    type: str = field(default="turn_start", init=False)

@dataclass(frozen=True)
class TurnEndEvent:
    turn: int
    duration_ms: float = 0.0
    type: str = field(default="turn_end", init=False)

@dataclass(frozen=True)
class ThinkingStartAgentEvent:
    index: int
    type: str = field(default="thinking_start", init=False)

@dataclass(frozen=True)
class ThinkingDeltaAgentEvent:
    delta: str
    type: str = field(default="thinking_delta", init=False)

@dataclass(frozen=True)
class ThinkingEndAgentEvent:
    index: int
    type: str = field(default="thinking_end", init=False)

@dataclass(frozen=True)
class TextStartAgentEvent:
    index: int
    type: str = field(default="text_start", init=False)

@dataclass(frozen=True)
class TextDeltaAgentEvent:
    delta: str
    type: str = field(default="text_delta", init=False)

@dataclass(frozen=True)
class TextEndAgentEvent:
    index: int
    type: str = field(default="text_end", init=False)

@dataclass(frozen=True)
class ToolDeclaredAgentEvent:
    """LLM has committed to this tool call (emitted during stream, before execution)."""
    id: str
    name: str
    args: dict
    type: str = field(default="tool_declared", init=False)

@dataclass(frozen=True)
class ToolExecutingAgentEvent:
    """About to call tool.execute() — emitted immediately before the tool runs."""
    id: str
    name: str
    args: dict
    type: str = field(default="tool_executing", init=False)

@dataclass(frozen=True)
class ToolResultAgentEvent:
    id: str
    name: str
    result: ToolResult
    type: str = field(default="tool_result", init=False)

@dataclass(frozen=True)
class ToolBlockedAgentEvent:
    id: str
    name: str
    reason: str
    type: str = field(default="tool_blocked", init=False)

@dataclass(frozen=True)
class ErrorAgentEvent:
    error: str
    retryable: bool
    type: str = field(default="error", init=False)

@dataclass(frozen=True)
class AbortedAgentEvent:
    type: str = field(default="aborted", init=False)

@dataclass(frozen=True)
class CompactionAgentEvent:
    tokens_before: int
    tokens_after: int
    type: str = field(default="compaction", init=False)

@dataclass(frozen=True)
class DoneAgentEvent:
    result: "AgentResult"
    type: str = field(default="done", init=False)

AgentEvent = (
    TurnStartEvent | TurnEndEvent |
    ThinkingStartAgentEvent | ThinkingDeltaAgentEvent | ThinkingEndAgentEvent |
    TextStartAgentEvent | TextDeltaAgentEvent | TextEndAgentEvent |
    ToolDeclaredAgentEvent | ToolExecutingAgentEvent |
    ToolResultAgentEvent | ToolBlockedAgentEvent |
    ErrorAgentEvent | AbortedAgentEvent |
    CompactionAgentEvent | DoneAgentEvent
)

# ── Internal: mutable turn result ─────────────────────────────────────────────
#
# _stream_one_turn is an async generator yielding AgentEvents live. It also
# needs to convey final turn state (assistant message, error, abort) back to
# agent_loop. We use a mutable _TurnResult passed by reference — the generator
# fills it before returning; agent_loop reads it after the generator is
# exhausted. This gives honest async-generator types (no sentinel crossing the
# boundary) and avoids the `isinstance(ev, _TurnOutcome)` / `break` pattern.

@dataclass
class _TurnResult:
    assistant_msg: AssistantMessage | None = None
    aborted: bool = False
    error: str | None = None

    @property
    def usage(self) -> TokenUsage:
        if self.assistant_msg is not None and self.assistant_msg.usage is not None:
            return self.assistant_msg.usage
        return ZERO_USAGE

    @property
    def tool_calls(self) -> list[ToolCallContent]:
        if self.assistant_msg is None:
            return []
        return [b for b in self.assistant_msg.content if isinstance(b, ToolCallContent)]

# ── ToolCallRecord (for action log) ──────────────────────────────────────────

@dataclass(frozen=True)
class ToolCallRecord:
    id: str
    name: str
    args: dict
    result: ToolResult
    blocked: bool = False
    block_reason: str | None = None

# ── Config & Result ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentConfig:
    model: Model
    system_prompt: list[SystemPromptSection]
    provider: AnthropicProvider          # carries api_key; replaces bare api_key field
    tool_registry: ToolRegistry
    thinking: str = "off"
    max_turns: int = 100  # Fix 4: raised from 50 to match coding-agent-flow
    max_tokens: int | None = None
    signal: asyncio.Event | None = None
    hooks: Hooks = field(default_factory=NoopHooks)

@dataclass(frozen=True)
class AgentResult:
    text: str
    tool_calls: list[ToolCallRecord]
    messages: list[Message]        # all assistant + tool_result messages in order
    usage: TokenUsage
    turns: int
    aborted: bool

# ── Hooks helpers ─────────────────────────────────────────────────────────────
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

# ── Tool result truncation ────────────────────────────────────────────────────

_MAX_TOOL_BYTES = 50 * 1024

def _truncate_tool_result(content: str) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= _MAX_TOOL_BYTES:
        return content
    return encoded[:_MAX_TOOL_BYTES].decode("utf-8", errors="ignore") + \
           "\n[Output truncated — use offset/limit to read more]"

# ── agent_loop ────────────────────────────────────────────────────────────────

async def agent_loop(
    config: AgentConfig,
    initial_messages: list[Message],
) -> AsyncGenerator[AgentEvent, None]:
    """
    Core agent loop as an async generator.

    Yields AgentEvents live (text streams, tool calls, results).
    Returns via DoneAgentEvent with AgentResult.

    Policies enforced:
      - Turn completeness: partial assistant messages are never appended on error/abort.
      - Tool result truncation: results > 50KB truncated before appending to context.
      - Retry: exponential backoff + jitter, up to 3 attempts per turn.
    """
    signal = config.signal
    abort = signal or asyncio.Event()

    conversation: list[Message] = list(initial_messages)
    all_tool_records: list[ToolCallRecord] = []
    all_result_messages: list[Message] = []
    total_usage = ZERO_USAGE
    turn_number = 0

    while turn_number < config.max_turns:
        turn_number += 1
        turn_start = time.perf_counter()
        yield TurnStartEvent(turn=turn_number)

        if abort.is_set():
            yield AbortedAgentEvent()
            return

        # Apply hooks
        messages_for_llm = await _hook_before_llm(config.hooks, conversation, turn_number)

        # Stream one LLM turn. _stream_one_turn fills `out` by reference;
        # we consume the generator fully, then read out.
        out = _TurnResult()
        async for ev in _stream_one_turn(config, messages_for_llm, turn_number, abort, out):
            yield ev

        if out.aborted:
            yield AbortedAgentEvent()
            return

        if out.error:
            yield ErrorAgentEvent(error=out.error, retryable=False)
            return

        assistant_msg: AssistantMessage = out.assistant_msg  # type: ignore[assignment]
        usage: TokenUsage = out.usage
        tool_calls: list[ToolCallContent] = out.tool_calls

        total_usage = total_usage + usage
        conversation.append(assistant_msg)
        all_result_messages.append(assistant_msg)

        # No tool calls → done
        if not tool_calls:
            yield TurnEndEvent(turn=turn_number, duration_ms=(time.perf_counter() - turn_start) * 1000)
            final_text = "".join(
                blk.text for blk in assistant_msg.content if isinstance(blk, TextContent)
            )
            result = AgentResult(
                text=final_text,
                tool_calls=all_tool_records,
                messages=list(all_result_messages),
                usage=total_usage,
                turns=turn_number,
                aborted=False,
            )
            yield DoneAgentEvent(result=result)
            return

        # Execute tool calls
        for i, call in enumerate(tool_calls):
            decision = await _hook_before_tool(config.hooks, call, turn_number)
            if decision.block:
                reason = decision.reason
                yield ToolBlockedAgentEvent(id=call.id, name=call.name, reason=reason or "blocked")
                tool_result = ToolResult(content=f"Tool call blocked: {reason}", is_error=True)
                all_tool_records.append(ToolCallRecord(
                    id=call.id, name=call.name, args=call.arguments,
                    result=tool_result, blocked=True, block_reason=reason,
                ))
                result_msg = ToolResultMessage(
                    tool_call_id=call.id, content=tool_result.content,
                    is_error=True, timestamp=int(time.time() * 1000),
                )
                conversation.append(result_msg)
                all_result_messages.append(result_msg)
                continue

            # Announce execution (two distinct moments: declared during stream, executing now)
            yield ToolExecutingAgentEvent(id=call.id, name=call.name, args=call.arguments)

            tool = config.tool_registry.get(call.name)
            if tool is None:
                tool_result = ToolResult(content=f"Unknown tool: {call.name}", is_error=True)
            else:
                try:
                    tool_result = await tool.execute(call.arguments, cwd=".", signal=abort)
                except Exception as exc:
                    tool_result = ToolResult(content=str(exc), is_error=True)

            yield ToolResultAgentEvent(id=call.id, name=call.name, result=tool_result)

            after = await _hook_after_tool(config.hooks, call, tool_result, turn_number)
            context_result = after if after is not None else tool_result

            truncated_content = _truncate_tool_result(context_result.content)
            result_msg = ToolResultMessage(
                tool_call_id=call.id, content=truncated_content,
                is_error=context_result.is_error, timestamp=int(time.time() * 1000),
            )
            conversation.append(result_msg)
            all_result_messages.append(result_msg)
            all_tool_records.append(ToolCallRecord(
                id=call.id, name=call.name, args=call.arguments, result=tool_result,
            ))

            if abort.is_set():
                # API requires every tool_use block in the AssistantMessage to have a
                # matching tool_result. Fill placeholder error results for all calls that
                # were not yet executed so the context stays valid on resume.
                for remaining in tool_calls[i + 1:]:
                    placeholder = ToolResult(content="Operation aborted", is_error=True)
                    result_msg = ToolResultMessage(
                        tool_call_id=remaining.id, content=placeholder.content,
                        is_error=True, timestamp=int(time.time() * 1000),
                    )
                    conversation.append(result_msg)
                    all_result_messages.append(result_msg)
                    all_tool_records.append(ToolCallRecord(
                        id=remaining.id, name=remaining.name, args=remaining.arguments,
                        result=placeholder,
                    ))
                yield AbortedAgentEvent()
                return

        yield TurnEndEvent(turn=turn_number, duration_ms=(time.perf_counter() - turn_start) * 1000)
        # Loop continues — tool results now in context, LLM called again

    # Max turns reached — emit DoneAgentEvent (aborted=True) so callers have a
    # single exit path. AgentResult.aborted distinguishes this from a clean finish.
    result = AgentResult(
        text="",
        tool_calls=all_tool_records,
        messages=list(all_result_messages),
        usage=total_usage,
        turns=turn_number,
        aborted=True,
    )
    yield DoneAgentEvent(result=result)


async def _stream_one_turn(
    config: AgentConfig,
    messages: list[Message],
    turn_number: int,
    abort: asyncio.Event,
    out: _TurnResult,
) -> AsyncGenerator[AgentEvent, None]:
    """
    Stream one LLM response, yielding AgentEvents *live* as the provider emits
    them. Fills `out` by reference on completion (success, abort, or error).

    Block lifecycle events from the provider (ContentBlockStartEvent /
    ContentBlockEndEvent) are translated to typed agent events:
      text block open/close   → TextStartAgentEvent / TextEndAgentEvent
      thinking block open/close → ThinkingStartAgentEvent / ThinkingEndAgentEvent
      tool_use block close    → ToolDeclaredAgentEvent (with full args)

    Live yielding is what makes the renderer responsive: thinking deltas and
    text deltas reach the UI as they arrive, not buffered to end-of-turn.

    Retry note: each attempt streams its own deltas live. A retryable error
    mid-stream emits a clear ErrorAgentEvent so the re-streamed response on
    the next attempt isn't confusing.
    """
    tool_defs: list[ToolDefinition] = config.tool_registry.definitions()

    for attempt in range(_MAX_RETRIES):
        try:
            async for ev in config.provider.stream(
                model=config.model,
                system=config.system_prompt,
                messages=messages,
                tools=tool_defs,
                signal=abort,
                max_tokens=config.max_tokens,
                thinking=config.thinking,
            ):
                if abort.is_set():
                    out.aborted = True
                    return

                if isinstance(ev, ContentBlockStartEvent):
                    if ev.block_type == "thinking":
                        yield ThinkingStartAgentEvent(index=ev.index)
                    elif ev.block_type == "text":
                        yield TextStartAgentEvent(index=ev.index)
                    # tool_use start: no agent event — ToolDeclaredAgentEvent fires at block end

                elif isinstance(ev, ContentBlockEndEvent):
                    if ev.block_type == "thinking":
                        yield ThinkingEndAgentEvent(index=ev.index)
                    elif ev.block_type == "text":
                        yield TextEndAgentEvent(index=ev.index)

                elif isinstance(ev, TextDeltaEvent):
                    yield TextDeltaAgentEvent(delta=ev.delta)

                elif isinstance(ev, ThinkingDeltaEvent):
                    yield ThinkingDeltaAgentEvent(delta=ev.delta)

                elif isinstance(ev, ToolCallEndEvent):
                    # LLM committed to this tool call — emit before we execute it
                    yield ToolDeclaredAgentEvent(id=ev.id, name=ev.name, args=ev.arguments)

                elif isinstance(ev, DoneEvent):
                    out.assistant_msg = ev.message
                    return

                elif isinstance(ev, StreamErrorEvent):
                    if ev.retryable and attempt < _MAX_RETRIES - 1:
                        delay = _retry_delay(attempt)
                        yield ErrorAgentEvent(
                            error=f"{ev.error} (retry {attempt+1}/{_MAX_RETRIES} in {delay:.0f}s)",
                            retryable=True,
                        )
                        await asyncio.sleep(delay)
                        break  # retry the outer attempt loop
                    out.error = ev.error
                    return

        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                delay = _retry_delay(attempt)
                yield ErrorAgentEvent(
                    error=f"{exc} (retry {attempt+1}/{_MAX_RETRIES} in {delay:.0f}s)",
                    retryable=True,
                )
                await asyncio.sleep(delay)
                continue
            out.error = str(exc)
            return

    out.error = "Max retries exceeded"


def make_config(
    model: Model,
    api_key: str | None,
    system_prompt: list[SystemPromptSection],
    tool_registry: ToolRegistry,
    cwd: str,
    thinking: str = "off",
    max_turns: int = 100,  # Fix 4: raised from 50
    project_root: str | None = None,
    max_tokens: int | None = None,
    signal: asyncio.Event | None = None,
    hooks: Hooks | None = None,
    *,
    provider: AnthropicProvider | None = None,
) -> AgentConfig:
    """
    Factory: build AgentConfig, injecting cwd into tool registry execution
    and encapsulating api_key inside an AnthropicProvider instance.

    Pass provider= directly (e.g. a mock) to skip AnthropicProvider construction —
    useful in tests. If provider is None, api_key must be supplied.
    """
    if provider is None:
        if not api_key:
            raise ValueError("Either api_key or provider must be supplied to make_config()")
        # Fix 10: project_root lets autonomous mode point to the repo even when
        # cwd is a worktree sibling that has no .agent-forge/ of its own.
        provider = AnthropicProvider(api_key, cwd=cwd, project_root=project_root)
    patched = _CwdPatchedRegistry(tool_registry, cwd)
    return AgentConfig(
        model=model,
        system_prompt=system_prompt,
        provider=provider,
        tool_registry=patched,
        thinking=thinking,
        max_turns=max_turns,
        max_tokens=max_tokens if max_tokens is not None else model.max_tokens,
        signal=signal,
        hooks=hooks if hooks is not None else NoopHooks(),
    )


async def run_agent(
    config: AgentConfig,
    initial_messages: list[Message],
    *,
    on_event: Any | None = None,
) -> AgentResult:
    """
    Convenience wrapper: drain agent_loop() and return the final AgentResult.

    on_event — optional async or sync callable(AgentEvent) called for every
               yielded event before run_agent processes it. Useful for logging
               or rendering in non-generator contexts.

    Raises RuntimeError on ErrorAgentEvent (fatal, non-retryable API failure).
    AbortedAgentEvent and max-turns both surface as AgentResult(aborted=True)
    via the terminal DoneAgentEvent.
    """
    async for event in agent_loop(config, initial_messages):
        if on_event is not None:
            if asyncio.iscoroutinefunction(on_event):
                await on_event(event)
            else:
                on_event(event)
        if isinstance(event, DoneAgentEvent):
            return event.result
        if isinstance(event, ErrorAgentEvent) and not event.retryable:
            raise RuntimeError(f"Agent error: {event.error}")
    # Unreachable — agent_loop always terminates with DoneAgentEvent or raises above.
    raise RuntimeError("agent_loop ended without DoneAgentEvent")


class _CwdPatchedRegistry(ToolRegistry):
    """Wraps a ToolRegistry, injecting cwd into every tool.execute() call."""

    def __init__(self, inner: ToolRegistry, cwd: str) -> None:
        super().__init__()
        self._inner = inner
        self._cwd = cwd

    def get(self, name: str):  # type: ignore[override]
        tool = self._inner.get(name)
        if tool is None:
            return None
        return _CwdBoundTool(tool, self._cwd)

    def definitions(self) -> list[ToolDefinition]:
        return self._inner.definitions()

    def names(self) -> list[str]:
        return self._inner.names()


class _CwdBoundTool:
    def __init__(self, tool: Any, cwd: str) -> None:
        self._tool = tool
        self._cwd = cwd

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def description(self) -> str:
        return self._tool.description

    @property
    def parameters(self) -> dict:
        return self._tool.parameters

    async def execute(self, args: dict, *, cwd: str = ".", signal=None) -> ToolResult:
        return await self._tool.execute(args, cwd=self._cwd, signal=signal)

    def definition(self) -> ToolDefinition:
        return self._tool.definition()
