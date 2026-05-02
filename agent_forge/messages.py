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
    text: str
    type: Literal["text"] = field(default="text", init=False)

@dataclass(frozen=True)
class ThinkingContent:
    thinking: str
    signature: str | None = None
    type: Literal["thinking"] = field(default="thinking", init=False)

@dataclass(frozen=True)
class ToolCallContent:
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
    content: str | tuple[TextContent, ...]
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["user"] = field(default="user", init=False)

@dataclass(frozen=True)
class AssistantMessage:
    content: tuple[ContentBlock, ...]
    stop_reason: str = "end_turn"   # "end_turn" | "tool_use" | "max_tokens" | "error"
    usage: "TokenUsage | None" = None
    model_id: str | None = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["assistant"] = field(default="assistant", init=False)

@dataclass(frozen=True)
class ToolResultMessage:
    tool_call_id: str
    content: str | tuple[TextContent | ImageContent, ...]
    is_error: bool = False
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["tool_result"] = field(default="tool_result", init=False)

Message = UserMessage | AssistantMessage | ToolResultMessage

# ── Token economics ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenUsage:
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
    content: str
    is_error: bool = False

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict

# ── System prompt section ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class SystemPromptSection:
    text: str
    cache_control: bool = False
