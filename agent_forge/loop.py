"""
loop.py — agent_loop() async generator + AgentEvent/AgentConfig/AgentResult types.

Depends on provider, tools, and context. Orchestrates one full task: context
builds the message array → loop calls the LLM via provider → tools execute tool
calls → loop appends results back to the message list. Session persistence is
deliberately excluded (that belongs to chat.py) so the loop stays a pure
generator that can be tested without disk I/O.

Owns: agent_loop() (the core async generator), all 12 AgentEvent frozen
      dataclasses, AgentConfig, AgentResult, make_config() (factory that injects
      cwd into the tool registry), _CwdPatchedRegistry / _CwdBoundTool, retry
      logic (_retry_delay), tool result truncation (_truncate_tool_result).

Policies enforced here:
  - Turn completeness: partial assistant messages are never appended on error/abort.
  - Tool result truncation: results > 50 KB truncated before appending to context.
  - Retry: exponential backoff + jitter, up to 3 attempts per turn, max 30 s.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from .provider import (
    AssistantMessage, DoneEvent, Message, Model, StreamErrorEvent,
    SystemPromptSection, TextContent, TextDeltaEvent, ThinkingContent,
    ThinkingDeltaEvent, ToolCallContent, ToolCallEndEvent, ToolDefinition,
    ToolResult, ToolResultMessage, TokenUsage, UserMessage, ZERO_USAGE,
    AnthropicProvider,
)
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0

def _retry_delay(attempt: int) -> float:
    return min(_BASE_DELAY * (2 ** attempt) * random.uniform(0.75, 1.25), _MAX_DELAY)

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
class TextDeltaAgentEvent:
    delta: str
    type: str = field(default="text_delta", init=False)

@dataclass(frozen=True)
class ThinkingDeltaAgentEvent:
    delta: str
    type: str = field(default="thinking_delta", init=False)

@dataclass(frozen=True)
class ToolCallStartAgentEvent:
    id: str
    name: str
    type: str = field(default="tool_call_start", init=False)

@dataclass(frozen=True)
class ToolCallEndAgentEvent:
    id: str
    name: str
    args: dict
    type: str = field(default="tool_call_end", init=False)

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
    TurnStartEvent | TurnEndEvent | TextDeltaAgentEvent | ThinkingDeltaAgentEvent |
    ToolCallStartAgentEvent | ToolCallEndAgentEvent | ToolResultAgentEvent |
    ToolBlockedAgentEvent | ErrorAgentEvent | AbortedAgentEvent |
    CompactionAgentEvent | DoneAgentEvent
)

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
    api_key: str
    tools: list[Any]               # list[Tool]
    tool_registry: ToolRegistry
    thinking: str = "off"
    max_turns: int = 50
    max_tokens: int | None = None
    signal: asyncio.Event | None = None
    hooks: Any | None = None       # Hooks object (optional)
    debug: bool = False

@dataclass(frozen=True)
class AgentResult:
    text: str
    tool_calls: list[ToolCallRecord]
    messages: list[Message]        # all assistant + tool_result messages in order
    usage: TokenUsage
    turns: int
    aborted: bool

# ── Hooks helpers ─────────────────────────────────────────────────────────────

async def _hook_before_llm(hooks: Any, messages: list[Message], turn: int) -> list[Message]:
    if hooks is None:
        return messages
    fn = getattr(hooks, "before_llm_call", None)
    if fn is None:
        return messages
    result = await fn(messages, {"turn": turn})
    return result if result is not None else messages

async def _hook_before_tool(hooks: Any, call: ToolCallContent, turn: int) -> tuple[bool, str | None]:
    if hooks is None:
        return False, None
    fn = getattr(hooks, "before_tool_call", None)
    if fn is None:
        return False, None
    result = await fn({"id": call.id, "name": call.name, "args": call.arguments}, {"turn": turn})
    if result is None:
        return False, None
    return result.get("block", False), result.get("reason")

async def _hook_after_tool(hooks: Any, call: ToolCallContent, result: ToolResult, turn: int) -> ToolResult | None:
    if hooks is None:
        return None
    fn = getattr(hooks, "after_tool_call", None)
    if fn is None:
        return None
    replacement = await fn({"id": call.id, "name": call.name, "args": call.arguments}, result, {"turn": turn})
    return replacement if isinstance(replacement, ToolResult) else None

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
    provider = AnthropicProvider()
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

        # Stream one LLM turn with retry
        outcome = await _stream_one_turn(
            provider, config, messages_for_llm, turn_number, abort,
        )

        if outcome["aborted"]:
            yield AbortedAgentEvent()
            return

        if outcome["error"]:
            # Yield collected error events then return
            for ev in outcome["events"]:
                yield ev
            yield ErrorAgentEvent(error=outcome["error"], retryable=False)
            return

        # Yield all live events from this turn
        for ev in outcome["events"]:
            yield ev

        assistant_msg: AssistantMessage = outcome["assistant_msg"]
        usage: TokenUsage = outcome["usage"]
        tool_calls: list[ToolCallContent] = outcome["tool_calls"]

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
        for call in tool_calls:
            yield ToolCallStartAgentEvent(id=call.id, name=call.name)
            yield ToolCallEndAgentEvent(id=call.id, name=call.name, args=call.arguments)

            blocked, reason = await _hook_before_tool(config.hooks, call, turn_number)
            if blocked:
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

            # Execute
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
                yield AbortedAgentEvent()
                return

        yield TurnEndEvent(turn=turn_number, duration_ms=(time.perf_counter() - turn_start) * 1000)
        # Loop continues — tool results now in context, LLM called again

    yield ErrorAgentEvent(error=f"Reached max turns ({config.max_turns})", retryable=False)


async def _stream_one_turn(
    provider: AnthropicProvider,
    config: AgentConfig,
    messages: list[Message],
    turn_number: int,
    abort: asyncio.Event,
) -> dict:
    """
    Stream one LLM response. Returns a dict with:
      events, assistant_msg, tool_calls, usage, aborted, error
    """
    tool_defs: list[ToolDefinition] = config.tool_registry.definitions()

    for attempt in range(_MAX_RETRIES):
        events: list[AgentEvent] = []
        tool_calls_in_progress: list[ToolCallContent] = []
        assistant_msg: AssistantMessage | None = None
        usage = ZERO_USAGE

        try:
            async for ev in provider.stream(
                model=config.model,
                system=config.system_prompt,
                messages=messages,
                tools=tool_defs,
                api_key=config.api_key,
                signal=abort,
                max_tokens=config.max_tokens,
                thinking=config.thinking,
            ):
                if abort.is_set():
                    return {"events": events, "aborted": True, "error": None,
                            "assistant_msg": None, "tool_calls": [], "usage": ZERO_USAGE}

                if isinstance(ev, TextDeltaEvent):
                    events.append(TextDeltaAgentEvent(delta=ev.delta))

                elif isinstance(ev, ThinkingDeltaEvent):
                    events.append(ThinkingDeltaAgentEvent(delta=ev.delta))

                elif isinstance(ev, ToolCallEndEvent):
                    tool_calls_in_progress.append(ToolCallContent(id=ev.id, name=ev.name, arguments=ev.arguments))

                elif isinstance(ev, DoneEvent):
                    assistant_msg = ev.message
                    usage = ev.usage
                    if not tool_calls_in_progress:
                        tool_calls_in_progress = [
                            blk for blk in (ev.message.content or ())
                            if isinstance(blk, ToolCallContent)
                        ]
                    return {
                        "events": events,
                        "assistant_msg": assistant_msg,
                        "tool_calls": tool_calls_in_progress,
                        "usage": usage,
                        "aborted": False,
                        "error": None,
                    }

                elif isinstance(ev, StreamErrorEvent):
                    if ev.retryable and attempt < _MAX_RETRIES - 1:
                        delay = _retry_delay(attempt)
                        events.append(ErrorAgentEvent(
                            error=f"{ev.error} (retry {attempt+1}/{_MAX_RETRIES} in {delay:.0f}s)",
                            retryable=True,
                        ))
                        await asyncio.sleep(delay)
                        break
                    return {"events": events, "error": ev.error, "aborted": False,
                            "assistant_msg": None, "tool_calls": [], "usage": ZERO_USAGE}

        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                delay = _retry_delay(attempt)
                events.append(ErrorAgentEvent(
                    error=f"{exc} (retry {attempt+1}/{_MAX_RETRIES} in {delay:.0f}s)",
                    retryable=True,
                ))
                await asyncio.sleep(delay)
                continue
            return {"events": events, "error": str(exc), "aborted": False,
                    "assistant_msg": None, "tool_calls": [], "usage": ZERO_USAGE}

    return {"events": [], "error": "Max retries exceeded", "aborted": False,
            "assistant_msg": None, "tool_calls": [], "usage": ZERO_USAGE}


def make_config(
    model: Model,
    api_key: str,
    system_prompt: list[SystemPromptSection],
    tool_registry: ToolRegistry,
    cwd: str,
    thinking: str = "off",
    max_turns: int = 50,
    max_tokens: int | None = None,
    signal: asyncio.Event | None = None,
    hooks: Any | None = None,
) -> AgentConfig:
    """Factory: build AgentConfig, injecting cwd into tool registry execution."""
    patched = _CwdPatchedRegistry(tool_registry, cwd)
    return AgentConfig(
        model=model,
        system_prompt=system_prompt,
        api_key=api_key,
        tools=[],
        tool_registry=patched,
        thinking=thinking,
        max_turns=max_turns,
        max_tokens=max_tokens if max_tokens is not None else model.max_tokens,
        signal=signal,
        hooks=hooks,
    )


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
