"""
FakeProvider — scripted LLMProvider for tests.

Records every stream() call (model/system/messages/tools/thinking) so tests
can assert what was sent to the provider, and yields a pre-built script of
StreamEvents in order. Mirrors the AnthropicProvider.stream() signature so it
can be passed via make_config(provider=...) without changing any other code.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from agent_forge.provider import (
    AssistantMessage, ContentBlockEndEvent, ContentBlockStartEvent,
    DoneEvent, Message, Model, StreamEvent, SystemPromptSection,
    TextContent, TextDeltaEvent, TokenUsage, ToolCallContent,
    ToolCallEndEvent, ToolDefinition,
)


@dataclass
class StreamCall:
    model: Model
    system: list[SystemPromptSection]
    messages: list[Message]
    tools: list[ToolDefinition]
    thinking: str
    max_tokens: int | None


class FakeProvider:
    """Stream a pre-scripted list of StreamEvents and record every call."""

    def __init__(self, scripts: list[list[StreamEvent]]):
        # scripts[i] = events yielded on the i-th stream() call
        self._scripts = list(scripts)
        self.calls: list[StreamCall] = []

    async def stream(
        self,
        model: Model,
        system: list[SystemPromptSection],
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        signal: asyncio.Event | None = None,
        max_tokens: int | None = None,
        thinking: str = "off",
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(StreamCall(
            model=model, system=list(system), messages=list(messages),
            tools=list(tools), thinking=thinking, max_tokens=max_tokens,
        ))
        if not self._scripts:
            raise AssertionError("FakeProvider: no script left for stream() call")
        events = self._scripts.pop(0)
        for ev in events:
            if signal is not None and signal.is_set():
                return
            yield ev


# ── Script builders ──────────────────────────────────────────────────────────

def text_turn(
    text: str,
    *,
    model_id: str = "claude-sonnet-4-6",
    usage: TokenUsage | None = None,
) -> list[StreamEvent]:
    """One assistant turn that emits text and stops (no tool calls)."""
    final = AssistantMessage(
        content=(TextContent(text=text),),
        stop_reason="end_turn",
        usage=usage or TokenUsage(input=10, output=5, cost=0.0),
        model_id=model_id,
    )
    return [
        ContentBlockStartEvent(index=0, block_type="text"),
        TextDeltaEvent(delta=text),
        ContentBlockEndEvent(index=0, block_type="text"),
        DoneEvent(message=final),
    ]


def tool_turn(
    tool_id: str,
    tool_name: str,
    args: dict,
    *,
    model_id: str = "claude-sonnet-4-6",
    preceding_text: str = "",
    usage: TokenUsage | None = None,
) -> list[StreamEvent]:
    """One assistant turn that emits an optional text block then a tool call."""
    blocks: list = []
    events: list[StreamEvent] = []
    idx = 0
    if preceding_text:
        events.append(ContentBlockStartEvent(index=idx, block_type="text"))
        events.append(TextDeltaEvent(delta=preceding_text))
        events.append(ContentBlockEndEvent(index=idx, block_type="text"))
        blocks.append(TextContent(text=preceding_text))
        idx += 1
    events.append(ContentBlockStartEvent(
        index=idx, block_type="tool_use", tool_id=tool_id, tool_name=tool_name,
    ))
    events.append(ToolCallEndEvent(id=tool_id, name=tool_name, arguments=args))
    blocks.append(ToolCallContent(id=tool_id, name=tool_name, arguments=args))
    final = AssistantMessage(
        content=tuple(blocks),
        stop_reason="tool_use",
        usage=usage or TokenUsage(input=10, output=5, cost=0.0),
        model_id=model_id,
    )
    events.append(DoneEvent(message=final))
    return events
