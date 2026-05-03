"""
events.py — AgentEvent value types yielded by agent_loop().

Depends only on messages and the ToolResult value type. Pulled out of loop.py
so the event surface (16 frozen dataclasses + ToolCallRecord + the AgentEvent
union) can be imported without dragging the loop algorithm — useful for
renderer.py and any external consumer.

Owns:
  TurnStartEvent, TurnEndEvent
  ThinkingStartAgentEvent, ThinkingDeltaAgentEvent, ThinkingEndAgentEvent
  TextStartAgentEvent, TextDeltaAgentEvent, TextEndAgentEvent
  ToolDeclaredAgentEvent  — LLM committed to a tool call (from stream, before exec)
  ToolExecutingAgentEvent — about to call tool.execute()
  ToolResultAgentEvent    — tool returned
  ToolBlockedAgentEvent   — hook blocked the call
  ErrorAgentEvent, AbortedAgentEvent, CompactionAgentEvent, DoneAgentEvent
  AgentEvent (union)
  ToolCallRecord (one entry of AgentResult.tool_calls)

Note: DoneAgentEvent.result is typed as `AgentResult` via a string forward
reference because AgentResult lives in loop.py (one level up). loop.py
imports DoneAgentEvent from here and resolves the forward reference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .messages import ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from .loop import AgentResult  # noqa: F401  (forward ref for DoneAgentEvent.result)


# ── Turn markers ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TurnStartEvent:
    turn: int
    type: str = field(default="turn_start", init=False)

@dataclass(frozen=True)
class TurnEndEvent:
    turn: int
    duration_ms: float = 0.0
    type: str = field(default="turn_end", init=False)


# ── Thinking block lifecycle ──────────────────────────────────────────────────

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


# ── Text block lifecycle ──────────────────────────────────────────────────────

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


# ── Tool lifecycle ────────────────────────────────────────────────────────────

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


# ── Terminal / error events ───────────────────────────────────────────────────

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


# ── ToolCallRecord (for AgentResult.tool_calls + action log) ──────────────────

@dataclass(frozen=True)
class ToolCallRecord:
    id: str
    name: str
    args: dict
    result: ToolResult
    blocked: bool = False
    block_reason: str | None = None
