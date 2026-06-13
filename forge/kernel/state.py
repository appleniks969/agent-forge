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
    AssistantTurn,
    Compacted,
    Envelope,
    Event,
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


# --- the single reducer -------------------------------------------------------


def apply_event(state: SessionState, event: Event) -> SessionState:
    """The ONE state transition. step() derives its next state by applying the
    events it emits through this function, and fold() applies the logged events
    through it — so fold == live is true by construction, not by test. Every
    state field changes here and nowhere else.

    Usage accumulates per model round (AssistantTurn / Compacted), so totals are
    exact at every point — not just at turn boundaries; TurnFinished only flips
    in_turn (it carries usage purely for the run record / footer)."""
    match event:
        case UserSubmitted(text):
            return replace(state, messages=state.messages + (UserMessage(text),))
        case TurnStarted(turn):
            return replace(state, turn=turn, round=0, turn_usage=Usage(), in_turn=True)
        case AssistantTurn(blocks, usage):
            # A round always counts (max-turns safety); an empty round adds no
            # message — no phantom AssistantMessage, the K1 fold/live divergence.
            msgs = state.messages + ((AssistantMessage(blocks),) if blocks else ())
            return replace(
                state,
                messages=msgs,
                round=state.round + 1,
                usage=state.usage + usage,
                turn_usage=state.turn_usage + usage,
            )
        case ToolDeclared(call):
            return replace(state, declared=state.declared + (call,))
        case ToolFinished(result):
            s = add_result(state, result)
            return flush_batch(s) if batch_complete(s) else s
        case PermissionAsked(question):
            return replace(
                state, pending_permissions=state.pending_permissions + (question,)
            )
        case PermissionDecided():
            return remove_permission(state, event.call_id)
        case Compacted():
            s = apply_compaction(state, event.summary)
            return replace(
                s, usage=s.usage + event.usage, turn_usage=s.turn_usage + event.usage
            )
        case TurnFinished():
            return replace(state, in_turn=False)
        case SessionEnded():
            return replace(state, finished=True)
        case _:
            return state  # ToolStarted, RetryScheduled, ChildSpawned: no state


# --- fold ---------------------------------------------------------------------


def fold(envelopes: Iterable[Envelope]) -> SessionState:
    """Reduce durable envelopes to SessionState; transient envelopes are skipped.

    A plain left fold over apply_event — the same function step() uses."""
    state = SessionState()
    for env in envelopes:
        if env.durable:
            state = apply_event(state, env.body)
    return state
