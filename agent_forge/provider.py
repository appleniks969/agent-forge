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

The AnthropicProvider concrete adapter lives in anthropic_provider.py and is
NOT re-exported here — provider.py is a Protocol-only seam. Import the concrete
adapter from `agent_forge` (top-level) or `agent_forge.anthropic_provider`.

For backward compatibility this module still re-exports the shared value types
(messages, tokens, tool plumbing, system sections, Model catalog) so existing
`from agent_forge.provider import UserMessage` imports keep working.
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
    """A new content block opened. Fired once per text/thinking/tool_use block."""
    index: int
    block_type: str              # "text" | "thinking" | "tool_use"
    tool_id: str | None = None
    tool_name: str | None = None

@dataclass(frozen=True)
class TextDeltaEvent:
    """A character chunk appended to the currently-open text block."""
    delta: str

@dataclass(frozen=True)
class ThinkingDeltaEvent:
    """A character chunk appended to the currently-open thinking block."""
    delta: str

@dataclass(frozen=True)
class ToolCallEndEvent:
    """A tool_use block closed; ``arguments`` is the fully-parsed JSON dict."""
    id: str
    name: str
    arguments: dict

@dataclass(frozen=True)
class ContentBlockEndEvent:
    """A text or thinking block closed. (tool_use uses ``ToolCallEndEvent`` instead.)"""
    index: int
    block_type: str              # "text" | "thinking"  (tool_use → ToolCallEndEvent)

@dataclass(frozen=True)
class DoneEvent:
    """Stream complete. ``message`` carries usage and the assembled assistant turn."""
    message: AssistantMessage

@dataclass(frozen=True)
class StreamErrorEvent:
    """A transient (``retryable=True``) or fatal error from the upstream provider."""
    error: str
    retryable: bool

StreamEvent = (
    ContentBlockStartEvent | TextDeltaEvent | ThinkingDeltaEvent
    | ToolCallEndEvent | ContentBlockEndEvent | DoneEvent | StreamErrorEvent
)

# ── LLMProvider Protocol ──────────────────────────────────────────────────────

@runtime_checkable
class LLMProvider(Protocol):
    """The seam between ``agent_loop`` and any concrete LLM adapter.

    Implementations adapt a vendor SDK (Anthropic, OpenAI, a fake test
    provider, …) into a stream of ``StreamEvent`` values. The loop never
    imports a concrete provider — it accepts any object satisfying this
    Protocol via ``AgentConfig.provider``.

    Reference implementations:
        - ``agent_forge.anthropic_provider.AnthropicProvider`` (Anthropic SDK)
        - ``tests/fake_provider.py`` (deterministic test double)

    Implementors must:
        - emit at most one ``DoneEvent`` per ``stream()`` invocation
        - emit ``ToolCallEndEvent`` for tool_use blocks (NOT ``ContentBlockEndEvent``)
        - respect ``signal`` and stop emitting promptly when set
        - never raise from inside ``stream()`` for transient failures — emit
          ``StreamErrorEvent(retryable=True)`` instead so the loop can retry
    """

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
        """Stream a single LLM turn as ``StreamEvent`` values.

        Args:
            model:      ``Model`` descriptor (id, context window, costs).
            system:     ordered list of system-prompt sections (cache hints honoured).
            messages:   conversation messages — user / assistant / tool_result.
            tools:      tool catalog the model may call.
            signal:     optional ``asyncio.Event``; set means "abort".
            max_tokens: soft cap on output tokens; provider may clamp further.
            thinking:   ``"off" | "adaptive" | "low" | "medium" | "high"``.

        Yields ``StreamEvent`` values, terminating with exactly one
        ``DoneEvent`` on success or a ``StreamErrorEvent`` on failure.
        """
        ...


