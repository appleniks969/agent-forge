"""
context.py — ContextWindow aggregate, pressure tiers, P4 eviction.

Depends only on provider. Sits between provider and loop: it manages *what the
LLM sees* (window policy, eviction, compaction) independently of *how a turn is
executed*. The loop calls build_messages() before each LLM call and receive()
after each turn. session.py is a sibling — neither imports the other.

SystemPrompt / SectionName moved to system_prompt.py (sibling module) — they
are a separate concern (ordered prompt sections + cache placement) and do not
share state with ContextWindow.

Owns: ContextWindow (ActionLog + RecencyWindow rotation), StratifiedWindowStrategy,
      PressureTier, assess_pressure(), evict_p4(), CompactionPort (DI boundary),
      token estimation helpers.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from .provider import (
    AssistantMessage, ImageContent, Message, Model,
    TextContent, ThinkingContent, ToolResultMessage, UserMessage,
)

# Re-export SystemPrompt / SectionName from their new home so existing imports
# `from agent_forge.context import SystemPrompt, SectionName` keep working.
from .system_prompt import SectionName, SystemPrompt  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)

# ── Pressure tiers ────────────────────────────────────────────────────────────

ABSOLUTE_P4 = 50_000   # start cheap truncation
ABSOLUTE_P3 = 100_000  # LLM compaction
ABSOLUTE_AGG = 200_000 # keep only last 2 turns

class PressureTier(enum.Enum):
    NONE = "none"
    P4   = "p4"
    P3   = "p3"
    AGG  = "aggressive"

def assess_pressure(tokens: int, model: Model) -> PressureTier:
    ratio = tokens / model.context_window if model.context_window > 0 else 0.0
    if tokens > ABSOLUTE_AGG or ratio > 0.95: return PressureTier.AGG
    if tokens > ABSOLUTE_P3  or ratio > 0.90: return PressureTier.P3
    if tokens > ABSOLUTE_P4  or ratio > 0.85: return PressureTier.P4
    return PressureTier.NONE

# ── Token estimation ──────────────────────────────────────────────────────────

def _tool_result_text(msg: ToolResultMessage) -> str:
    """Return string representation of ToolResultMessage.content for token estimation."""
    if isinstance(msg.content, str):
        return msg.content
    # Structured content (TextContent / ImageContent blocks)
    parts: list[str] = []
    for blk in msg.content:
        if isinstance(blk, TextContent):
            parts.append(blk.text)
        elif isinstance(blk, ImageContent):
            parts.append(f"[image/{blk.media_type}]")
    return " ".join(parts)

def estimate_tokens(msg: Message) -> int:
    """Chars / 4 heuristic. Replaced by sync_total_tokens() after each API call."""
    if isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, str):
            return len(content) // 4
        return sum(len(c.text) for c in content) // 4
    elif isinstance(msg, AssistantMessage):
        total = 0
        for blk in msg.content:
            total += len(getattr(blk, "text", "") or getattr(blk, "thinking", "") or str(getattr(blk, "arguments", ""))) // 4
        return total
    else:  # ToolResultMessage
        return len(_tool_result_text(msg)) // 4

def estimate_tokens_list(msgs: list[Message]) -> int:
    return sum(estimate_tokens(m) for m in msgs)

# ── ContextBudget ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContextBudget:
    keep_recent_tokens: int
    recency_turns: int

def default_budget(model: Model) -> ContextBudget:
    return ContextBudget(
        keep_recent_tokens=min(int(model.context_window * 0.10), 40_000),
        recency_turns=10,
    )

# ── Window strategy ───────────────────────────────────────────────────────────

_TRUNC_SUFFIX = "\n[...truncated — use Read to re-examine]"
_RECENCY_MAX_CHARS = 2_000

@dataclass
class TurnRecord:
    turn: int
    user_message: Message
    assistant_messages: list[Message]
    tool_calls: list           # list of ToolCallRecord (for action log)
    tokens: int

@dataclass(frozen=True)
class ActionLogEntry:
    turn: int
    summary: str
    tokens: int


class StratifiedWindowStrategy:
    """Two-layer context: ActionLog (one-liners) + RecencyWindow (last K turns)."""

    def build(
        self,
        action_log: list[ActionLogEntry],
        recent_turns: list[TurnRecord],
        current_user_message: Message,
    ) -> list[Message]:
        msgs: list[Message] = []

        if action_log:
            log_text = "\n".join(e.summary for e in action_log)
            msgs.append(UserMessage(content=f"[Prior session actions]\n{log_text}", timestamp=0))
            msgs.append(AssistantMessage(
                content=(TextContent(text="Understood. I have the context from prior actions."),),
                timestamp=0,
            ))

        newest_idx = len(recent_turns) - 1
        for i, rec in enumerate(recent_turns):
            msgs.append(rec.user_message)
            is_newest = (i == newest_idx)
            for msg in rec.assistant_messages:
                if is_newest:
                    msgs.append(msg)
                elif isinstance(msg, AssistantMessage):
                    stripped = tuple(b for b in msg.content if not isinstance(b, ThinkingContent))
                    if stripped:
                        msgs.append(AssistantMessage(content=stripped, timestamp=msg.timestamp))
                elif isinstance(msg, ToolResultMessage) and isinstance(msg.content, str) and len(msg.content) > _RECENCY_MAX_CHARS:
                    msgs.append(ToolResultMessage(
                        tool_call_id=msg.tool_call_id,
                        content=msg.content[:_RECENCY_MAX_CHARS] + _TRUNC_SUFFIX,
                        is_error=msg.is_error, timestamp=msg.timestamp,
                    ))
                else:
                    msgs.append(msg)

        msgs.append(current_user_message)
        return msgs

    def summarise_turn(self, rec: TurnRecord) -> ActionLogEntry:
        content = getattr(rec.user_message, "content", "")
        user_text = content[:80].replace("\n", " ") if isinstance(content, str) else f"Turn {rec.turn}"
        actions = _summarise_tool_calls(rec.tool_calls)
        summary = f"[T{rec.turn}] {user_text} → {actions}" if actions else f"[T{rec.turn}] {user_text}"
        return ActionLogEntry(turn=rec.turn, summary=summary, tokens=max(1, len(summary) // 4))


def _summarise_tool_calls(tool_calls: list) -> str:
    parts: list[str] = []
    for tc in tool_calls:
        name = getattr(tc, "name", "?")
        args = getattr(tc, "args", {})
        result = getattr(tc, "result", None)
        is_err = getattr(result, "is_error", False) if result else False
        if name == "Read":   parts.append(f"Read {args.get('path', '?')}")
        elif name == "Edit": parts.append(f"Edited {args.get('path', '?')}")
        elif name == "Write":parts.append(f"Wrote {args.get('path', '?')}")
        elif name == "Bash": parts.append(f"Bash: {str(args.get('command',''))[:40]} ({'err' if is_err else 'ok'})")
        elif name == "Grep": parts.append(f"Grep \"{str(args.get('pattern',''))[:30]}\"")
        elif name == "Find": parts.append(f"Find {args.get('pattern','?')}")
        else:                parts.append(name)
    return ", ".join(parts)

# ── P4 eviction ───────────────────────────────────────────────────────────────

_P4_MAX_BYTES = 1024  # 1 KB — in-place eviction threshold for historical tool results
_P4_NOTICE = "[tool result evicted — use Read to re-examine if needed]"

def evict_p4(msgs: list[Message]) -> list[Message]:
    """Truncate ToolResultMessages > 1KB. Used on historical (non-newest) turns."""
    result: list[Message] = []
    for msg in msgs:
        if isinstance(msg, ToolResultMessage):
            # Only evict string content; structured content (images) is kept as-is.
            content_bytes = msg.content.encode() if isinstance(msg.content, str) else b""
            if len(content_bytes) > _P4_MAX_BYTES:
                result.append(ToolResultMessage(
                    tool_call_id=msg.tool_call_id, content=_P4_NOTICE,
                    is_error=msg.is_error, timestamp=msg.timestamp,
                ))
                continue
        result.append(msg)
    return result

# ── CompactionPort protocol ───────────────────────────────────────────────────

@dataclass(frozen=True)
class CompactionResult:
    messages: list[Message]
    summary: str

class CompactionPort:
    async def compact(self, messages: list[Message], keep_recent_turns: int) -> CompactionResult:
        raise NotImplementedError

# ── ContextWindow ─────────────────────────────────────────────────────────────

class ContextWindow:
    """
    Aggregate: manages the stratified context window for one session.

    Invariants:
      1. RecencyWindow tokens <= budget.keep_recent_tokens (enforced after each receive()).
      2. At least 1 turn always kept in the recency window.
      3. ActionLog accumulates one-liner summaries of all evicted turns.
      4. CompactionPort is optional — P3/AGG fall back to P4 if absent.

    Token accounting:
      estimate_tokens() uses a chars/4 heuristic by default.
      Call sync_total_tokens(n) after each API response to replace the heuristic
      with the real API-reported total (input + cache_read). This gives accurate
      pressure-tier assessment and correct context-% display in the toolbar.
    """

    def __init__(
        self,
        model: Model,
        budget: ContextBudget | None = None,
        compaction_port: CompactionPort | None = None,
    ) -> None:
        self._model = model
        self._budget = budget or default_budget(model)
        self._strategy = StratifiedWindowStrategy()
        self._port = compaction_port
        self._action_log: list[ActionLogEntry] = []
        self._recent_turns: list[TurnRecord] = []
        self.current_turn: int = 0
        self._synced_total: int | None = None  # real API token count when available

    # ── Build LLM message array ───────────────────────────────────────────────

    def build_messages(self, current_user_message: Message) -> list[Message]:
        return self._strategy.build(self._action_log, self._recent_turns, current_user_message)

    # ── Receive completed turn ────────────────────────────────────────────────

    def receive(
        self,
        user_message: Message,
        assistant_messages: list[Message],
        tool_calls: list,
    ) -> None:
        """
        Called after each agent turn. Rotates the recency window.

        Token accounting uses the chars/4 heuristic here; call
        sync_total_tokens() separately with the real API count so that
        estimate_tokens() stays accurate across turns.
        """
        self.current_turn += 1
        # Reset the synced total — it belongs to the previous full-context state;
        # the new turn has added messages so we need a fresh sync from the API.
        self._synced_total = None
        turn_tokens = estimate_tokens(user_message) + estimate_tokens_list(assistant_messages)

        rec = TurnRecord(
            turn=self.current_turn,
            user_message=user_message,
            assistant_messages=list(assistant_messages),
            tool_calls=list(tool_calls),
            tokens=turn_tokens,
        )
        self._recent_turns.append(rec)

        # Evict oldest turns that exceed budget — always keep at least 1
        while len(self._recent_turns) > 1:
            total = sum(t.tokens for t in self._recent_turns)
            over_tok = total > self._budget.keep_recent_tokens
            over_cap = len(self._recent_turns) > self._budget.recency_turns
            if over_tok or over_cap:
                evicted = self._recent_turns.pop(0)
                self._action_log.append(self._strategy.summarise_turn(evicted))
            else:
                break

    # ── Token estimate ────────────────────────────────────────────────────────

    def sync_total_tokens(self, total: int) -> None:
        """
        Replace the heuristic estimate with the real API-reported token count.
        Call this after each agent turn with (usage.input + usage.cache_read).
        The value is used by estimate_tokens() until the next receive() resets it.
        """
        self._synced_total = total

    def estimate_tokens(self) -> int:
        """Return token estimate. Uses real API count if sync_total_tokens() was called."""
        if self._synced_total is not None:
            return self._synced_total
        log_tokens = sum(e.tokens for e in self._action_log)
        recent_tokens = sum(t.tokens for t in self._recent_turns)
        overhead = 20 if log_tokens > 0 else 0
        return log_tokens + recent_tokens + overhead

    # ── Pressure tier ─────────────────────────────────────────────────────────

    def pressure_tier(self) -> PressureTier:
        return assess_pressure(self.estimate_tokens(), self._model)

    # ── P4 in-place eviction ──────────────────────────────────────────────────

    def apply_eviction(self) -> None:
        """Truncate large tool results in all but the newest turn. Zero LLM cost.

        Resets _synced_total: eviction changes the window so the previous
        API-reported count is stale; estimate_tokens() will use the heuristic
        until the next sync_total_tokens() call.
        """
        for i in range(max(0, len(self._recent_turns) - 1)):
            turn = self._recent_turns[i]
            turn.assistant_messages = evict_p4(turn.assistant_messages)
            turn.tokens = estimate_tokens(turn.user_message) + estimate_tokens_list(turn.assistant_messages)
        self._synced_total = None   # evicted content changed the window; old API count is stale

    # ── Managed pressure ─────────────────────────────────────────────────────

    async def manage_pressure(self) -> PressureTier:
        tier = self.pressure_tier()
        if tier in (PressureTier.P3, PressureTier.AGG):
            if self._port is not None:
                try:
                    keep = 2 if tier == PressureTier.AGG else 6
                    await self._compact(keep)
                except Exception:
                    logger.warning("Compaction failed; falling back to P4", exc_info=True)
                    self.apply_eviction()
            else:
                self.apply_eviction()
        elif tier == PressureTier.P4:
            self.apply_eviction()
        return tier

    async def _compact(self, keep_recent_turns: int) -> None:
        if self._port is None:
            raise RuntimeError("No CompactionPort injected")
        flat = [msg for t in self._recent_turns for msg in [t.user_message] + t.assistant_messages]
        if not flat:
            return
        result = await self._port.compact(flat, keep_recent_turns)
        saved_log = list(self._action_log)
        self.clear()
        self._action_log = saved_log
        self.init_from_existing(result.messages)

    # ── Session resume ────────────────────────────────────────────────────────

    def init_from_existing(self, messages: list[Message]) -> None:
        """Token-based bucketing on session resume. Oldest turns become ActionLog."""
        boundaries = [i for i, m in enumerate(messages) if isinstance(m, UserMessage)]
        if not boundaries:
            return

        token_accum = 0
        recency_start = len(boundaries)

        for t in range(len(boundaries) - 1, -1, -1):
            start = boundaries[t]
            end = boundaries[t + 1] if t + 1 < len(boundaries) else len(messages)
            turn_tokens = estimate_tokens_list(messages[start:end])
            if (token_accum + turn_tokens > self._budget.keep_recent_tokens
                    or (len(boundaries) - t) > self._budget.recency_turns) and recency_start < len(boundaries):
                break
            token_accum += turn_tokens
            recency_start = t

        for t in range(recency_start):
            start = boundaries[t]
            end = boundaries[t + 1] if t + 1 < len(boundaries) else len(messages)
            user_msg = messages[start]
            content = getattr(user_msg, "content", "")
            user_text = (content[:80].replace("\n", " ")) if isinstance(content, str) else f"Turn {t+1}"
            self._action_log.append(ActionLogEntry(
                turn=t + 1, summary=f"[T{t+1}] {user_text} (resumed)", tokens=15,
            ))

        for t in range(recency_start, len(boundaries)):
            self.current_turn += 1
            start = boundaries[t]
            end = boundaries[t + 1] if t + 1 < len(boundaries) else len(messages)
            turn_msgs = messages[start:end]
            if not turn_msgs:
                continue
            self._recent_turns.append(TurnRecord(
                turn=self.current_turn,
                user_message=turn_msgs[0],
                assistant_messages=list(turn_msgs[1:]),
                tool_calls=[],
                tokens=estimate_tokens_list(turn_msgs),
            ))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def clear(self) -> None:
        self._action_log = []
        self._recent_turns = []
        self.current_turn = 0
        self._synced_total = None

