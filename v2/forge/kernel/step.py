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
    AssistantBlock,
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
from forge.kernel.state import (
    SessionState,
    add_result,
    append_assistant,
    apply_compaction,
    batch_complete,
    flush_batch,
    remove_permission,
)
from forge.kernel.types import (
    Allow,
    Ask,
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
    Usage,
    UserMessage,
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
    new = replace(
        state,
        messages=state.messages + (UserMessage(inp.text),),
        turn=state.turn + 1,
        round=0,
        turn_usage=Usage(),
        in_turn=True,
    )
    events: tuple[Event, ...] = (UserSubmitted(inp.text), TurnStarted(new.turn))
    new, effects, more = _continue_turn(new, policy, allow_compaction=True)
    return Step(new, effects, events + more)


def _on_model(state: SessionState, inp: ModelResponded, policy: Policy) -> Step:
    if not state.in_turn:
        return Step(state, (), ())
    usage = inp.output.usage
    new = replace(state, usage=state.usage + usage, turn_usage=state.turn_usage + usage)

    if inp.request.purpose == "compaction":
        summary = inp.output.text
        new = apply_compaction(new, summary)
        events: tuple[Event, ...] = (Compacted(summary=summary, first_kept_seq=0),)
        # No re-check of should_compact: one compaction per pressure crossing.
        new, effects, more = _continue_turn(new, policy, allow_compaction=False)
        return Step(new, effects, events + more)

    blocks = inp.output.blocks
    new = append_assistant(new, blocks)
    events = tuple(AssistantBlock(b) for b in blocks)

    calls = inp.output.tool_calls
    if not calls:
        new, effects, more = _finish_turn(new, "ok", text=inp.output.text)
        return Step(new, effects, events + more)

    # Declare the whole batch first so fold's completeness check (results ==
    # declared) cannot fire before every call is on the books.
    new = replace(new, declared=calls, results=())
    events += tuple(ToolDeclared(c) for c in calls)

    spec_effects = {t.name: t.effects for t in inp.request.tools}
    to_run: list[ToolCall] = []
    asks: list[AskUser] = []
    for call in calls:
        verdict = policy.judge(call, spec_effects.get(call.name, Effects(0)))
        match verdict:
            case Allow():
                to_run.append(call)
                events += (ToolStarted(call.id),)
            case Deny(reason):
                result = ToolResult(call.id, f"permission denied: {reason}", is_error=True)
                events += (
                    PermissionDecided(call.id, allowed=False, source="policy", reason=reason),
                    ToolFinished(result),
                )
                new = add_result(new, result)
            case Ask(question):
                if question.call_id != call.id:
                    question = replace(question, call_id=call.id)
                events += (PermissionAsked(question),)
                new = replace(
                    new, pending_permissions=new.pending_permissions + (question,)
                )
                asks.append(AskUser(question))

    effects = tuple(asks)
    if to_run:
        effects += (RunTools(tuple(to_run)),)
    if batch_complete(new):  # every call denied at judge time
        new = flush_batch(new)
        new, more_eff, more_ev = _continue_turn(new, policy, allow_compaction=True)
        return Step(new, effects + more_eff, events + more_ev)
    return Step(new, effects, events)


def _on_outcome(state: SessionState, inp: ToolOutcome, policy: Policy) -> Step:
    pending_ids = {c.id for c in state.pending_calls}
    if not state.in_turn or inp.result.call_id not in pending_ids:
        return Step(state, (), ())
    result = _truncate(inp.result, policy.result_cap_bytes)
    new = add_result(state, result)
    events: tuple[Event, ...] = (ToolFinished(result),)
    if batch_complete(new):
        new = flush_batch(new)
        new, effects, more = _continue_turn(new, policy, allow_compaction=True)
        return Step(new, effects, events + more)
    return Step(new, (), events)


def _on_answer(state: SessionState, inp: PermissionAnswer, policy: Policy) -> Step:
    question = next(
        (q for q in state.pending_permissions if q.call_id == inp.call_id), None
    )
    if not state.in_turn or question is None:
        return Step(state, (), ())
    new = remove_permission(state, inp.call_id)
    events: tuple[Event, ...] = (
        PermissionDecided(inp.call_id, allowed=inp.allow, source="user", reason=""),
    )
    if inp.allow:
        call = next(c for c in new.declared if c.id == inp.call_id)
        events += (ToolStarted(call.id),)
        return Step(new, (RunTools((call,)),), events)
    result = ToolResult(inp.call_id, "permission denied by user", is_error=True)
    events += (ToolFinished(result),)
    new = add_result(new, result)
    if batch_complete(new):
        new = flush_batch(new)
        new, effects, more = _continue_turn(new, policy, allow_compaction=True)
        return Step(new, effects, events + more)
    return Step(new, (), events)


def _on_cancel(state: SessionState) -> Step:
    if not state.in_turn:
        return Step(replace(state, finished=True), (), (SessionEnded(),))
    events: tuple[Event, ...] = ()
    new = state
    for question in new.pending_permissions:
        events += (
            PermissionDecided(
                question.call_id, allowed=False, source="policy", reason="cancelled"
            ),
        )
    new = replace(new, pending_permissions=())
    for call in new.pending_calls:
        placeholder = ToolResult(call.id, CANCELLED_CONTENT, is_error=True)
        events += (ToolFinished(placeholder),)
        new = add_result(new, placeholder)
    if batch_complete(new):
        new = flush_batch(new)
    new, effects, more = _finish_turn(new, "aborted")
    return Step(new, effects, events + more)


# --- shared turn continuation -------------------------------------------------


def _continue_turn(
    state: SessionState, policy: Policy, *, allow_compaction: bool
) -> tuple[SessionState, tuple[Effect, ...], tuple[Event, ...]]:
    if state.round >= policy.max_turns:
        return _finish_turn(state, "max_turns")
    if allow_compaction and policy.should_compact(state):
        request = policy.build_request(state, "compaction")
        return state, (CallModel(request, "compaction"),), ()
    request = policy.build_request(state, "turn")
    return state, (CallModel(request, "turn"),), ()


def _finish_turn(
    state: SessionState, outcome: Outcome, text: str = ""
) -> tuple[SessionState, tuple[Effect, ...], tuple[Event, ...]]:
    # Cost is None from the kernel: pricing is provider knowledge (ModelInfo);
    # external consumers derive cost from the logged usage.
    events: tuple[Event, ...] = (TurnFinished(outcome, state.turn_usage, None),)
    new = replace(state, in_turn=False)
    return new, (Finish(TurnResult(outcome, text)),), events


def _truncate(result: ToolResult, cap_bytes: int) -> ToolResult:
    raw = result.content.encode("utf-8", errors="replace")
    if len(raw) <= cap_bytes:
        return result
    kept = raw[:cap_bytes].decode("utf-8", errors="ignore")
    return replace(result, content=kept + TRUNCATION_MARKER)
