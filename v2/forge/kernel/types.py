"""Pure value types shared by every layer.

Layer: kernel — imports nothing internal, stdlib only. Every conversation,
provider, tool, and permission value object lives here as a frozen dataclass;
ports contain only Protocols over these types.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Flag, StrEnum, auto
from typing import Any, Literal

Purpose = Literal["turn", "compaction"]
Outcome = Literal["ok", "aborted", "max_turns", "fatal"]
DecisionSource = Literal["user", "policy"]


class Effort(StrEnum):
    NONE = "none"
    LOW = "low"
    MED = "med"
    HIGH = "high"


class Stability(StrEnum):
    STATIC = "static"
    SESSION = "session"
    VOLATILE = "volatile"


class Effects(Flag):
    READ_PATH = auto()
    WRITE_PATH = auto()
    EXEC = auto()
    NETWORK = auto()
    EXTERNAL = auto()  # EXTERNAL => default verdict is Ask


# --- content blocks -------------------------------------------------------


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ThinkingBlock:
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: Mapping[str, Any]


Block = TextBlock | ThinkingBlock | ToolCall


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


# --- messages -------------------------------------------------------------


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    blocks: tuple[Block, ...]

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolCall))


@dataclass(frozen=True)
class ToolResultMessage:
    results: tuple[ToolResult, ...]


Message = UserMessage | AssistantMessage | ToolResultMessage


# --- accounting -----------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens; None pricing on ModelInfo means cost is omitted."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * self.cache_read_per_mtok
            + usage.cache_write_tokens * self.cache_write_per_mtok
        ) / 1_000_000


# --- tools ----------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: Mapping[str, Any] = field(default_factory=dict)  # JSON Schema
    effects: Effects = Effects(0)


# --- prompts and provider I/O ----------------------------------------------


@dataclass(frozen=True)
class PromptSection:
    name: str
    text: str
    stability: Stability


@dataclass(frozen=True)
class ModelRequest:
    model: str
    system: tuple[PromptSection, ...]
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    effort: Effort
    purpose: Purpose


@dataclass(frozen=True)
class ModelOutput:
    blocks: tuple[Block, ...]
    usage: Usage
    stop_reason: str = "end_turn"

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolCall))

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context_tokens: int
    pricing: Pricing | None
    efforts: frozenset[Effort]


@dataclass(frozen=True)
class Delta:
    """One streamed fragment; render-only, never enters kernel state."""

    kind: Literal["text", "thinking"]
    text: str


# --- permissions ------------------------------------------------------------


@dataclass(frozen=True)
class PermissionQuestion:
    call_id: str
    tool: str
    question: str


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Ask:
    question: PermissionQuestion


Verdict = Allow | Deny | Ask


# --- turn outcome -----------------------------------------------------------


@dataclass(frozen=True)
class TurnResult:
    outcome: Outcome
    text: str = ""
