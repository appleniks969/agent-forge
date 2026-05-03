"""
messages.py — shared value types: content blocks, Messages, TokenUsage, tool plumbing.

Zero internal dependencies. Every other module that needs to reference a Message
type, a TokenUsage, a ToolResult, or a SystemPromptSection imports from here —
not from provider.py. This avoids dragging the Anthropic SDK adapter into the
import graph of modules that only need value types.

Owns: TextContent, ThinkingContent, ToolCallContent, ImageContent (ContentBlock
      union), UserMessage, AssistantMessage, ToolResultMessage (Message union),
      TokenUsage, ZERO_USAGE, ToolResult, ToolDefinition, SystemPromptSection.

All public types are @dataclass(frozen=True). Timestamps default to int(ms)
from time.time() on construction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

# ── Content blocks ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TextContent:
    """A plain-text content block. Used inside both user and assistant messages."""
    text: str
    type: Literal["text"] = field(default="text", init=False)

@dataclass(frozen=True)
class ThinkingContent:
    """An extended-thinking block emitted by reasoning models.

    ``signature`` is the provider-supplied opaque token that must be returned
    on follow-up turns to keep the thinking valid; preserve it verbatim.
    """
    thinking: str
    signature: str | None = None
    type: Literal["thinking"] = field(default="thinking", init=False)

@dataclass(frozen=True)
class ToolCallContent:
    """A tool_use block: the model's request to invoke ``name`` with ``arguments``.

    ``id`` is the provider-assigned correlation id; the matching
    ``ToolResultMessage`` must carry the same id in ``tool_call_id``.
    """
    id: str
    name: str
    arguments: dict
    type: Literal["tool_use"] = field(default="tool_use", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", dict(self.arguments))

@dataclass(frozen=True)
class ImageContent:
    """Base64-encoded image for vision tool results."""
    media_type: str          # "image/png" | "image/jpeg" | "image/webp" | "image/gif"
    data: str                # base64-encoded bytes
    type: Literal["image"] = field(default="image", init=False)

ContentBlock = TextContent | ThinkingContent | ToolCallContent | ImageContent

# ── Messages ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UserMessage:
    """User-authored input. content can be a plain string or a tuple of
    TextContent / ImageContent blocks (vision inputs piggyback on the same
    type — the LLM sees them in user-role messages, not tool-result messages,
    when the user pastes an image directly)."""
    content: str | tuple[TextContent | ImageContent, ...]
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["user"] = field(default="user", init=False)

@dataclass(frozen=True)
class AssistantMessage:
    """One assistant turn: ordered content blocks plus stop reason and usage.

    ``content`` is a tuple of ``ContentBlock`` values — typically
    ``ThinkingContent*`` then ``TextContent*`` then ``ToolCallContent*`` in the
    order the provider emitted them. ``stop_reason`` distinguishes a natural
    end (``"end_turn"``), tool-call hand-off (``"tool_use"``), token cap
    (``"max_tokens"``), or upstream error (``"error"``). ``usage`` is the
    real provider-reported token counts; ``None`` means the loop hasn't
    populated it yet (e.g. mid-stream).
    """
    content: tuple[ContentBlock, ...]
    stop_reason: str = "end_turn"   # "end_turn" | "tool_use" | "max_tokens" | "error"
    usage: "TokenUsage | None" = None
    model_id: str | None = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["assistant"] = field(default="assistant", init=False)

@dataclass(frozen=True)
class ToolResultMessage:
    """Result of executing one tool call. Carries the ``tool_call_id`` it answers.

    ``content`` is the tool's textual output (or a vision-tuple for image-aware
    results). ``is_error=True`` flags execution failures so the model can
    distinguish them from normal output.
    """
    tool_call_id: str
    content: str | tuple[TextContent | ImageContent, ...]
    is_error: bool = False
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["tool_result"] = field(default="tool_result", init=False)

Message = UserMessage | AssistantMessage | ToolResultMessage

# ── Token economics ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenUsage:
    """Per-turn token counts and accumulated USD cost.

    All four token fields are provider-reported; ``cost`` is computed locally
    from the ``Model.cost`` table. ``__add__`` is provided so per-turn usage
    can be summed into a session total via ``sum(usages, ZERO_USAGE)``.
    """
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cost=self.cost + other.cost,
        )

ZERO_USAGE = TokenUsage()

# ── Tool plumbing ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolResult:
    """A tool's return value. ``is_error=True`` indicates execution failure.

    Tools always return this rather than raising. The agent loop wraps the
    ``content`` into a ``ToolResultMessage`` for the next turn.
    """
    content: str
    is_error: bool = False

@dataclass(frozen=True)
class ToolDefinition:
    """Provider-facing tool schema: name, description, JSON-Schema parameters.

    Returned by ``Tool.definition()`` and forwarded to the LLM. The model
    decides when (and how) to call the tool based on these three fields alone.
    """
    name: str
    description: str
    parameters: dict

# ── System prompt section ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class SystemPromptSection:
    """One ordered section of the system prompt.

    `cache_control` is an *advisory hint* that the active provider may map to
    a vendor-specific cache breakpoint (Anthropic ephemeral cache_control,
    OpenAI prompt-cache, etc.) — or may ignore entirely. The name is kept for
    backward compatibility; new code should prefer the alias `hint_cache`.
    """
    text: str
    cache_control: bool = False

    @property
    def hint_cache(self) -> bool:
        """Forward-compatible alias for cache_control."""
        return self.cache_control
