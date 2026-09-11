"""step(state, input, policy) -> Step — the pure kernel transition.

Layer: kernel — imports kernel siblings only; stdlib otherwise. step() is the
ONLY event producer in the system. It owns conversation-validity invariants
and nothing else: matched tool_use/tool_result pairs, placeholder results on
cancel, max-turns, result-size caps, permission bookkeeping, and the
compaction transition. Retry timing, rendering, and persistence live in the
driver and adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from forge.kernel.events import (
    AssistantTurn,
    Compacted,
    Event,
    PermissionAsked,
    PermissionDecided,
    SessionEnded,
    ToolDeclared,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UserSubmitted,
)
from forge.kernel.state import SessionState, apply_event
from forge.kernel.types import (
    Allow,
    Ask,
    Block,
    Deny,
    Effects,
    ModelOutput,
    ModelRequest,
    Outcome,
    PermissionQuestion,
    Purpose,
    ToolCall,
    ToolResult,
    TurnResult,
    Verdict,
)

TRUNCATION_MARKER = "\n[truncated]"
CANCELLED_CONTENT = "cancelled"


# --- inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class UserInput:
    text: str


@dataclass(frozen=True)
class ModelResponded:
    """A completed model output re-entering the kernel.

    Carries the originating request so the kernel can read purpose and the
    declared ToolSpec effects without holding tool registries in state.
    """

    output: ModelOutput
    request: ModelRequest


@dataclass(frozen=True)
class ToolOutcome:
    result: ToolResult


@dataclass(frozen=True)
class PermissionAnswer:
    call_id: str
    allow: bool


@dataclass(frozen=True)
class Cancelled:
    pass


Input = UserInput | ModelResponded | ToolOutcome | PermissionAnswer | Cancelled


# --- effects --------------------------------------------------------------------


@dataclass(frozen=True)
class CallModel:
    request: ModelRequest
    purpose: Purpose


@dataclass(frozen=True)
class RunTools:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class AskUser:
    question: PermissionQuestion


@dataclass(frozen=True)
class Finish:
    result: TurnResult


Effect = CallModel | RunTools | AskUser | Finish


@dataclass(frozen=True)
class Step:
    state: SessionState
    effects: tuple[Effect, ...]
    events: tuple[Event, ...]


class Policy(Protocol):
    @property
    def result_cap_bytes(self) -> int: ...

    @property
    def max_turns(self) -> int: ...

    def build_request(self, state: SessionState, purpose: Purpose) -> ModelRequest: ...

    def judge(self, call: ToolCall, effects: Effects) -> Verdict: ...

    def should_compact(self, state: SessionState) -> bool: ...


# --- the transition ---------------------------------------------------------------


class _Build:
    """Threads state and events together: every emitted event is applied to the
    running state via apply_event — the SAME reducer fold uses. So a step's
    returned state is, by construction, the fold of the events it emits."""

    __slots__ = ("state", "events")

    def __init__(self, state: SessionState) -> None:
        self.state = state
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
        self.state = apply_event(self.state, event)

    def done(self, effects: tuple[Effect, ...] = ()) -> Step:
        return Step(self.state, effects, tuple(self.events))


def step(state: SessionState, inp: Input, policy: Policy) -> Step:
    if state.finished:
        return Step(state, (), ())
    match inp:
        case UserInput():
            return _on_user(state, inp, policy)
        case ModelResponded():
            return _on_model(state, inp, policy)
        case ToolOutcome():
            return _on_outcome(state, inp, policy)
        case PermissionAnswer():
            return _on_answer(state, inp, policy)
        case Cancelled():
            return _on_cancel(state)
    return Step(state, (), ())


def _on_user(state: SessionState, inp: UserInput, policy: Policy) -> Step:
    if state.in_turn:
        return Step(state, (), ())  # mid-turn input is a driver bug; ignore purely
    b = _Build(state)
    b.emit(UserSubmitted(inp.text))
    b.emit(TurnStarted(state.turn + 1))
    return _continue(b, policy, allow_compaction=True)


def _on_model(state: SessionState, inp: ModelResponded, policy: Policy) -> Step:
    if not state.in_turn:
        return Step(state, (), ())
    b = _Build(state)
    if inp.request.purpose == "compaction":
        summary = inp.output.text
        if not summary.strip():
            # Empty summary would wipe the window via apply_compaction; skip
            # the event so the live messages survive and fold stays honest.
            return _continue(b, policy, allow_compaction=False)
        b.emit(Compacted(summary=summary, first_kept_seq=0, usage=inp.output.usage))
        # One compaction per pressure crossing: don't re-offer it this round.
        return _continue(b, policy, allow_compaction=False)

    # Duplicate tool-call ids (unvalidated provider passthrough) would wedge
    # the turn forever: pending_calls dedupes by id while batch_complete
    # counts results, so declared=2/results=1 never completes. Dedupe blocks
    # BEFORE any event is emitted so fold and live replay identically.
    blocks = _dedupe_call_ids(inp.output.blocks)
    b.emit(AssistantTurn(blocks, inp.output.usage))
    calls = tuple(blk for blk in blocks if isinstance(blk, ToolCall))
    if not calls:
        return _finish(b, "ok", text=inp.output.text)

    # Declare the whole batch before any verdict, so the completeness check
    # (results == declared) cannot fire before every call is on the books.
    for call in calls:
        b.emit(ToolDeclared(call))

    spec_effects = {t.name: t.effects for t in inp.request.tools}
    to_run: list[ToolCall] = []
    asks: list[AskUser] = []
    for call in calls:
        verdict = policy.judge(call, spec_effects.get(call.name, Effects(0)))
        match verdict:
            case Allow():
                to_run.append(call)
                b.emit(ToolStarted(call.id))
            case Deny(reason):
                b.emit(
                    PermissionDecided(call.id, allowed=False, source="policy", reason=reason)
                )
                b.emit(
                    ToolFinished(
                        ToolResult(call.id, f"permission denied: {reason}", is_error=True)
                    )
                )
            case Ask(question):
                if question.call_id != call.id:
                    question = replace(question, call_id=call.id)
                b.emit(PermissionAsked(question))
                asks.append(AskUser(question))

    effects: tuple[Effect, ...] = tuple(asks)
    if to_run:
        effects += (RunTools(tuple(to_run)),)
    if not b.state.declared:  # every call denied -> apply_event flushed the batch
        return _continue(b, policy, effects=effects, allow_compaction=True)
    return b.done(effects)


def _on_outcome(state: SessionState, inp: ToolOutcome, policy: Policy) -> Step:
    pending_ids = {c.id for c in state.pending_calls}
    if not state.in_turn or inp.result.call_id not in pending_ids:
        return Step(state, (), ())
    b = _Build(state)
    had_batch = bool(state.declared)
    b.emit(ToolFinished(_truncate(inp.result, policy.result_cap_bytes)))
    if had_batch and not b.state.declared:  # batch completed and flushed
        return _continue(b, policy, allow_compaction=True)
    return b.done()


def _on_answer(state: SessionState, inp: PermissionAnswer, policy: Policy) -> Step:
    question = next(
        (q for q in state.pending_permissions if q.call_id == inp.call_id), None
    )
    if not state.in_turn or question is None:
        return Step(state, (), ())
    b = _Build(state)
    b.emit(PermissionDecided(inp.call_id, allowed=inp.allow, source="user", reason=""))
    if inp.allow:
        call = next((c for c in b.state.declared if c.id == inp.call_id), None)
        if call is None:  # the call already left the batch — nothing to run
            return b.done()
        b.emit(ToolStarted(call.id))
        return b.done((RunTools((call,)),))
    had_batch = bool(state.declared)
    b.emit(ToolFinished(ToolResult(inp.call_id, "permission denied by user", is_error=True)))
    if had_batch and not b.state.declared:
        return _continue(b, policy, allow_compaction=True)
    return b.done()


def _on_cancel(state: SessionState) -> Step:
    b = _Build(state)
    if not state.in_turn:
        b.emit(SessionEnded())
        return b.done()
    for question in state.pending_permissions:
        b.emit(
            PermissionDecided(
                question.call_id, allowed=False, source="policy", reason="cancelled"
            )
        )
    for call in state.pending_calls:
        b.emit(ToolFinished(ToolResult(call.id, CANCELLED_CONTENT, is_error=True)))
    return _finish(b, "aborted")


# --- shared turn continuation -------------------------------------------------


def _continue(
    b: _Build, policy: Policy, *, effects: tuple[Effect, ...] = (), allow_compaction: bool
) -> Step:
    if b.state.round >= policy.max_turns:
        return _finish(b, "max_turns", effects=effects)
    purpose: Purpose = (
        "compaction" if allow_compaction and policy.should_compact(b.state) else "turn"
    )
    request = policy.build_request(b.state, purpose)
    return b.done(effects + (CallModel(request, purpose),))


def _finish(
    b: _Build, outcome: Outcome, *, text: str = "", effects: tuple[Effect, ...] = ()
) -> Step:
    # Cost is None from the kernel: pricing is provider knowledge (ModelInfo);
    # external consumers derive cost from the logged usage. TurnFinished carries
    # the turn's usage for the run record; apply_event only flips in_turn.
    b.emit(TurnFinished(outcome, b.state.turn_usage, None))
    return b.done(effects + (Finish(TurnResult(outcome, text)),))


def _dedupe_call_ids(blocks: tuple[Block, ...]) -> tuple[Block, ...]:
    """Keep the first tool call per id; drop exact-id duplicates. Non-call
    blocks pass through untouched."""
    seen: set[str] = set()
    out: list[Block] = []
    for blk in blocks:
        if isinstance(blk, ToolCall):
            if blk.id in seen:
                continue
            seen.add(blk.id)
        out.append(blk)
    return tuple(out)


def _truncate(result: ToolResult, cap_bytes: int) -> ToolResult:
    raw = result.content.encode("utf-8", errors="replace")
    if len(raw) <= cap_bytes:
        return result
    kept = raw[:cap_bytes].decode("utf-8", errors="ignore")
    return replace(result, content=kept + TRUNCATION_MARKER)
