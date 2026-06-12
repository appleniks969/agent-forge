"""SessionState and fold(envelopes) -> SessionState.

Layer: kernel — imports kernel siblings only. The append-only log owns truth:
fold over durable envelopes reproduces the live state step() maintains, so
resume and live are the same function. fold == live is guaranteed at turn
boundaries (every scenario ends in TurnFinished / SessionEnded); mid-turn,
usage totals lag until the TurnFinished event lands.

The batch/flush/compaction helpers here are shared by step() and fold so the
two transitions cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from forge.kernel.events import (
    AssistantBlock,
    Compacted,
    Envelope,
    PermissionAsked,
    PermissionDecided,
    SessionEnded,
    ToolDeclared,
    ToolFinished,
    TurnFinished,
    TurnStarted,
    UserSubmitted,
)
from forge.kernel.types import (
    AssistantMessage,
    Block,
    Message,
    PermissionQuestion,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    Usage,
    UserMessage,
)


@dataclass(frozen=True)
class SessionState:
    messages: tuple[Message, ...] = ()
    declared: tuple[ToolCall, ...] = ()  # current batch, in declaration order
    results: tuple[ToolResult, ...] = ()  # current batch results, in arrival order
    pending_permissions: tuple[PermissionQuestion, ...] = ()
    turn: int = 0  # user turns started
    round: int = 0  # model rounds completed within the current turn
    usage: Usage = Usage()  # session totals
    turn_usage: Usage = Usage()  # current turn only; reset at TurnStarted
    summary: str | None = None  # latest compaction summary
    in_turn: bool = False
    finished: bool = False

    @property
    def pending_calls(self) -> tuple[ToolCall, ...]:
        done = {r.call_id for r in self.results}
        return tuple(c for c in self.declared if c.id not in done)

    @property
    def pending_permission(self) -> PermissionQuestion | None:
        return self.pending_permissions[0] if self.pending_permissions else None


# --- transition helpers shared by step() and fold ----------------------------


def batch_complete(state: SessionState) -> bool:
    return bool(state.declared) and len(state.results) == len(state.declared)


def add_result(state: SessionState, result: ToolResult) -> SessionState:
    return replace(state, results=state.results + (result,))


def flush_batch(state: SessionState) -> SessionState:
    """Append one ToolResultMessage in declaration order; clear the batch."""
    by_id = {r.call_id: r for r in state.results}
    ordered = tuple(by_id[c.id] for c in state.declared)
    return replace(
        state,
        messages=state.messages + (ToolResultMessage(ordered),),
        declared=(),
        results=(),
    )


def remove_permission(state: SessionState, call_id: str) -> SessionState:
    kept = tuple(q for q in state.pending_permissions if q.call_id != call_id)
    return replace(state, pending_permissions=kept)


def apply_compaction(state: SessionState, summary: str) -> SessionState:
    """The old window is replaced by the summary; nothing pre-compaction survives."""
    return replace(state, messages=(), summary=summary)


def append_assistant(state: SessionState, blocks: tuple[Block, ...]) -> SessionState:
    return replace(
        state,
        messages=state.messages + (AssistantMessage(blocks),),
        round=state.round + 1,
    )


# --- fold ---------------------------------------------------------------------


def fold(envelopes: Iterable[Envelope]) -> SessionState:
    """Reduce durable envelopes to SessionState; transient envelopes are skipped."""
    state = SessionState()
    # AssistantBlock events for one model output arrive as a consecutive run;
    # buffer them and flush as one AssistantMessage when the run ends.
    buf: list[Block] = []
    for env in envelopes:
        if not env.durable:
            continue
        state, buf = _apply(state, env.body, buf)
    if buf:
        state = append_assistant(state, tuple(buf))
    return state


def _apply(state: SessionState, event: object, buf: list[Block]) -> tuple[SessionState, list[Block]]:
    if isinstance(event, AssistantBlock):
        return state, buf + [event.block]
    if buf:
        state = append_assistant(state, tuple(buf))
        buf = []
    match event:
        case UserSubmitted(text):
            state = replace(state, messages=state.messages + (UserMessage(text),))
        case TurnStarted(turn):
            state = replace(state, turn=turn, round=0, turn_usage=Usage(), in_turn=True)
        case ToolDeclared(call):
            state = replace(state, declared=state.declared + (call,))
        case ToolFinished(result):
            state = add_result(state, result)
            if batch_complete(state):
                state = flush_batch(state)
        case PermissionAsked(question):
            state = replace(
                state, pending_permissions=state.pending_permissions + (question,)
            )
        case PermissionDecided():
            state = remove_permission(state, event.call_id)
        case Compacted():
            state = apply_compaction(state, event.summary)
        case TurnFinished():
            state = replace(
                state,
                usage=state.usage + event.usage,
                turn_usage=event.usage,
                in_turn=False,
            )
        case SessionEnded():
            state = replace(state, finished=True)
        case _:
            pass  # ToolStarted, RetryScheduled, ChildSpawned carry no state
    return state, buf
