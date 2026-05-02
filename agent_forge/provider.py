"""
provider.py — LLMProvider Protocol + StreamEvent union.

The Protocol seam between the agent loop and any concrete LLM adapter.
Depends on messages (value types) and models (Model descriptor).

Owns: StreamEvent dataclasses (ContentBlockStartEvent, TextDeltaEvent,
      ThinkingDeltaEvent, ToolCallEndEvent, ContentBlockEndEvent, DoneEvent,
      StreamErrorEvent) and the LLMProvider Protocol that yields them.

Stream event hierarchy (7 events, block-lifecycle-aware):
  ContentBlockStartEvent  — a new content block opened (text/thinking/tool_use)
  TextDeltaEvent          — text character chunk
  ThinkingDeltaEvent      — thinking character chunk
  ToolCallEndEvent        — tool_use block closed (full args parsed)
  ContentBlockEndEvent    — a text or thinking block closed
  DoneEvent               — message complete (usage embedded in AssistantMessage)
  StreamErrorEvent        — transient or fatal error

The AnthropicProvider concrete adapter lives in anthropic_provider.py.
For backward compatibility this module re-exports every shared value type
(messages, tokens, tool plumbing, system sections, Model catalog) plus
AnthropicProvider — so existing `from agent_forge.provider import …`
imports keep working unchanged.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# ── Re-exports for back-compat ────────────────────────────────────────────────
#
# Internal modules SHOULD import value types from .messages / .models directly
# (see CHANGED below). External callers using `from agent_forge.provider import
# X` continue to resolve the same names through these re-exports.

from .messages import (  # noqa: F401  (re-exports)
    AssistantMessage, ContentBlock, ImageContent, Message,
    SystemPromptSection, TextContent, ThinkingContent, ToolCallContent,
    ToolDefinition, ToolResult, ToolResultMessage, TokenUsage, UserMessage,
    ZERO_USAGE,
)
from .models import (  # noqa: F401  (re-exports)
    DEFAULT_MODEL, MODELS, Model, ModelCost,
)

# ── Stream events ─────────────────────────────────────────────────────────────
#
# Seven events, aligned to Anthropic block lifecycle:
#
#   ContentBlockStartEvent  fired at content_block_start for every block type
#   TextDeltaEvent          fired for each text_delta
#   ThinkingDeltaEvent      fired for each thinking_delta
#   ToolCallEndEvent        fired at content_block_stop for tool_use (args parsed)
#   ContentBlockEndEvent    fired at content_block_stop for text / thinking
#   DoneEvent               fired at message_stop (usage embedded in AssistantMessage)
#   StreamErrorEvent        transient or fatal error

@dataclass(frozen=True)
class ContentBlockStartEvent:
    index: int
    block_type: str              # "text" | "thinking" | "tool_use"
    tool_id: str | None = None
    tool_name: str | None = None

@dataclass(frozen=True)
class TextDeltaEvent:
    delta: str

@dataclass(frozen=True)
class ThinkingDeltaEvent:
    delta: str

@dataclass(frozen=True)
class ToolCallEndEvent:
    id: str
    name: str
    arguments: dict

@dataclass(frozen=True)
class ContentBlockEndEvent:
    index: int
    block_type: str              # "text" | "thinking"  (tool_use → ToolCallEndEvent)

@dataclass(frozen=True)
class DoneEvent:
    message: AssistantMessage

@dataclass(frozen=True)
class StreamErrorEvent:
    error: str
    retryable: bool

StreamEvent = (
    ContentBlockStartEvent | TextDeltaEvent | ThinkingDeltaEvent
    | ToolCallEndEvent | ContentBlockEndEvent | DoneEvent | StreamErrorEvent
)

# ── LLMProvider Protocol ──────────────────────────────────────────────────────

@runtime_checkable
class LLMProvider(Protocol):
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
    ) -> AsyncIterator[StreamEvent]: ...


# ── AnthropicProvider re-export ──────────────────────────────────────────────

from .anthropic_provider import AnthropicProvider  # noqa: E402, F401
