"""Context window policy: recency selection, action-log eviction, pressure tiers.

Layer: policy — pure and synchronous, imports kernel only. Selects what the
model sees per call (recency window + one-liner action log for evicted turns),
assesses pressure tiers, estimates tokens (chars/4 heuristic calibrated from
real Usage on logged ModelOutputs), and decides when truncation can no longer
relieve pressure and LLM compaction is needed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from forge.kernel.types import (
    AssistantMessage,
    Block,
    Message,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

_CHARS_PER_TOKEN = 4
# Calibration is clamped so one anomalous Usage report cannot send every
# subsequent estimate to zero or to 16x reality.
_SCALE_MIN = 0.25
_SCALE_MAX = 4.0

EVICTED_NOTICE = "[tool result evicted - re-run the tool if needed]"
_SUMMARY_HEADER = "[Conversation summary]"
_ACTION_LOG_HEADER = "[Prior actions]"
_ACK_TEXT = "Understood; continuing with that context."


# --- pressure tiers -----------------------------------------------------------


class PressureTier(StrEnum):
    NONE = "none"
    EVICT = "evict"  # cheap truncation of historical tool results
    COMPACT = "compact"  # LLM compaction warranted
    CRITICAL = "critical"


@dataclass(frozen=True)
class ContextBudget:
    """Thresholds are absolute-OR-ratio so small test windows exercise the
    ratio path while production windows hit the absolute floors first."""

    context_tokens: int
    recency_turns: int = 10
    keep_recent_tokens: int = 40_000
    evict_result_bytes: int = 1_024
    evict_at: int = 50_000
    compact_at: int = 100_000
    critical_at: int = 200_000
    evict_ratio: float = 0.85
    compact_ratio: float = 0.90
    critical_ratio: float = 0.95


def default_budget(context_tokens: int) -> ContextBudget:
    return ContextBudget(
        context_tokens=context_tokens,
        keep_recent_tokens=max(1, min(int(context_tokens * 0.10), 40_000)),
    )


def assess_pressure(tokens: int, budget: ContextBudget) -> PressureTier:
    ratio = tokens / budget.context_tokens if budget.context_tokens > 0 else 0.0
    if tokens > budget.critical_at or ratio > budget.critical_ratio:
        return PressureTier.CRITICAL
    if tokens > budget.compact_at or ratio > budget.compact_ratio:
        return PressureTier.COMPACT
    if tokens > budget.evict_at or ratio > budget.evict_ratio:
        return PressureTier.EVICT
    return PressureTier.NONE


# --- token estimation -----------------------------------------------------------


@dataclass(frozen=True)
class TokenEstimator:
    """chars/4 heuristic scaled by a calibration factor learned from Usage.

    Calibration is a value transformation, not hidden state: recalibrated()
    returns a new estimator, so token sync stays a fold input rather than a
    scattered invariant.
    """

    scale: float = 1.0

    def text(self, text: str) -> int:
        return self._scaled(len(text))

    def message(self, msg: Message) -> int:
        match msg:
            case UserMessage(text):
                chars = len(text)
            case AssistantMessage(blocks):
                chars = sum(_block_chars(b) for b in blocks)
            case ToolResultMessage(results):
                chars = sum(len(r.content) for r in results)
            case _:
                chars = 0
        return max(1, self._scaled(chars))

    def messages(self, msgs: Iterable[Message]) -> int:
        return sum(self.message(m) for m in msgs)

    def recalibrated(self, estimated_tokens: int, usage: Usage) -> TokenEstimator:
        """Correct the scale given a real Usage observed for a window this
        estimator measured at estimated_tokens. Real context size at the call
        is input + cache-read tokens."""
        real = usage.input_tokens + usage.cache_read_tokens
        if estimated_tokens <= 0 or real <= 0:
            return self
        scale = self.scale * (real / estimated_tokens)
        return TokenEstimator(scale=min(_SCALE_MAX, max(_SCALE_MIN, scale)))

    def _scaled(self, chars: int) -> int:
        return math.ceil(chars / _CHARS_PER_TOKEN * self.scale)


def _block_chars(block: Block) -> int:
    if isinstance(block, ToolCall):
        return len(block.name) + sum(
            len(str(k)) + len(str(v)) for k, v in block.args.items()
        )
    return len(block.text)


# --- window selection -----------------------------------------------------------


@dataclass(frozen=True)
class Window:
    messages: tuple[Message, ...]  # exactly what the model will see
    action_log: tuple[str, ...]  # one-liners for evicted turns
    estimated_tokens: int
    pressure: PressureTier


def select_window(
    messages: Sequence[Message],
    *,
    summary: str | None = None,
    budget: ContextBudget,
    estimator: TokenEstimator = TokenEstimator(),
) -> Window:
    """Recency window over whole turns; evicted turns become action-log lines.

    Turns are never split (matched tool_use/tool_result pairs stay intact) and
    the newest turn is always kept whole. Under pressure, oversized tool
    results in kept-but-historical turns are replaced with a notice; the
    newest turn is never touched.
    """
    segments = _segment(messages)
    seg_tokens = [estimator.messages(seg) for seg in segments]

    keep_from = 0
    while len(segments) - keep_from > 1 and (
        len(segments) - keep_from > budget.recency_turns
        or sum(seg_tokens[keep_from:]) > budget.keep_recent_tokens
    ):
        keep_from += 1

    kept = [list(seg) for seg in segments[keep_from:]]
    action_log = tuple(_one_liner(i + 1, segments[i]) for i in range(keep_from))

    rendered = _render(summary, action_log, [m for seg in kept for m in seg])
    estimated = estimator.messages(rendered)
    pressure = assess_pressure(estimated, budget)

    if pressure is not PressureTier.NONE and len(kept) > 1:
        kept = [
            _evict_results(seg, budget.evict_result_bytes) for seg in kept[:-1]
        ] + [kept[-1]]
        rendered = _render(summary, action_log, [m for seg in kept for m in seg])
        estimated = estimator.messages(rendered)
        pressure = assess_pressure(estimated, budget)

    return Window(
        messages=tuple(rendered),
        action_log=action_log,
        estimated_tokens=estimated,
        pressure=pressure,
    )


def should_compact(
    messages: Sequence[Message],
    *,
    summary: str | None = None,
    budget: ContextBudget,
    estimator: TokenEstimator = TokenEstimator(),
) -> bool:
    """True when even the truncated window stays at COMPACT pressure or above —
    i.e. pressure the context policy cannot relieve without an LLM summary."""
    window = select_window(
        messages, summary=summary, budget=budget, estimator=estimator
    )
    return window.pressure in (PressureTier.COMPACT, PressureTier.CRITICAL)


# --- internals -------------------------------------------------------------------


def _segment(messages: Sequence[Message]) -> list[list[Message]]:
    """Split on UserMessage boundaries; a leading user-less run (e.g. right
    after compaction emptied the window) forms its own oldest segment."""
    segments: list[list[Message]] = []
    current: list[Message] = []
    for msg in messages:
        if isinstance(msg, UserMessage) and current:
            segments.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        segments.append(current)
    return segments


def _render(
    summary: str | None,
    action_log: tuple[str, ...],
    kept: list[Message],
) -> list[Message]:
    context_parts: list[str] = []
    if summary:
        context_parts.append(f"{_SUMMARY_HEADER}\n{summary}")
    if action_log:
        context_parts.append(_ACTION_LOG_HEADER + "\n" + "\n".join(action_log))
    if not context_parts:
        return list(kept)
    out: list[Message] = [UserMessage("\n\n".join(context_parts))]
    # The ack keeps user/assistant alternation only when the kept window
    # resumes at a user turn; a user-less continuation follows naturally.
    if kept and isinstance(kept[0], UserMessage):
        out.append(AssistantMessage((TextBlock(_ACK_TEXT),)))
    out.extend(kept)
    return out


def _evict_results(segment: list[Message], max_bytes: int) -> list[Message]:
    out: list[Message] = []
    for msg in segment:
        if isinstance(msg, ToolResultMessage):
            results = tuple(
                replace(r, content=EVICTED_NOTICE)
                if len(r.content.encode("utf-8", errors="replace")) > max_bytes
                else r
                for r in msg.results
            )
            out.append(ToolResultMessage(results))
        else:
            out.append(msg)
    return out


def _one_liner(turn: int, segment: list[Message]) -> str:
    user = next((m for m in segment if isinstance(m, UserMessage)), None)
    head = " ".join(user.text.split())[:60] if user else "(continuation)"
    results = {
        r.call_id: r
        for m in segment
        if isinstance(m, ToolResultMessage)
        for r in m.results
    }
    frags: list[str] = []
    for msg in segment:
        if isinstance(msg, AssistantMessage):
            for call in msg.tool_calls:
                result = results.get(call.id)
                err = "!" if result is not None and result.is_error else ""
                frags.append(f"{call.name}({_arg_fragment(call.args)}){err}")
    line = f"[T{turn}] {head}"
    if frags:
        line += " -> " + ", ".join(frags)
    return line


def _arg_fragment(args: object) -> str:
    values = args.values() if hasattr(args, "values") else ()
    for value in values:
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:40]
    return ""
