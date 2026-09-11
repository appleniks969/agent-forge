"""Table-driven tests for the pure kernel transition step().

No mocks, no asyncio: assert step(state, input, policy) == expected shapes.
"""

from __future__ import annotations

from collections import deque

import pytest
from conftest import (
    SimPolicy,
    Script,
    assert_matched_pairs,
    kinds,
    mk_out,
    simulate,
)

from forge.kernel.events import (
    Compacted,
    PermissionAsked,
    PermissionDecided,
    ToolFinished,
    TurnFinished,
)
from forge.kernel.state import SessionState
from forge.kernel.step import (
    TRUNCATION_MARKER,
    AskUser,
    CallModel,
    Cancelled,
    Finish,
    ModelResponded,
    PermissionAnswer,
    RunTools,
    Step,
    ToolOutcome,
    UserInput,
    step,
)
from forge.kernel.types import (
    AssistantMessage,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    TurnResult,
    Usage,
    UserMessage,
)

C1 = ToolCall("c1", "read", {"path": "a.py"})
C2 = ToolCall("c2", "read", {"path": "b.py"})


def start_turn(policy: SimPolicy, text: str = "go") -> Step:
    return step(SessionState(), UserInput(text), policy)


# --- the full simulated turn ---------------------------------------------------


def test_full_turn_user_model_tools_finish() -> None:
    policy = SimPolicy()
    script = Script(
        outputs=deque(
            [
                mk_out(TextBlock("looking"), C1, C2, usage=Usage(10, 5)),
                mk_out(TextBlock("done"), usage=Usage(20, 7)),
            ]
        ),
        tool_results={"c1": ToolResult("c1", "alpha"), "c2": ToolResult("c2", "beta")},
    )
    run = simulate(policy, script)

    assert kinds(run.events) == [
        "UserSubmitted",
        "TurnStarted",
        "AssistantTurn",  # one event per model round: text + c1 + c2
        "ToolDeclared",
        "ToolDeclared",
        "ToolStarted",
        "ToolStarted",
        "ToolFinished",
        "ToolFinished",
        "AssistantTurn",  # final round: text
        "TurnFinished",
    ]
    assert run.finishes == [TurnResult("ok", "done")]
    state = run.state
    assert state.turn == 1
    assert state.round == 2
    assert not state.in_turn
    assert not state.finished
    assert state.usage == Usage(30, 12)
    assert state.turn_usage == Usage(30, 12)
    assert isinstance(state.messages[0], UserMessage)
    assert isinstance(state.messages[1], AssistantMessage)
    assert state.messages[2] == ToolResultMessage(
        (ToolResult("c1", "alpha"), ToolResult("c2", "beta"))
    )
    assert isinstance(state.messages[3], AssistantMessage)
    assert_matched_pairs(state)
    # the judge saw the spec-declared effects, not tool-name strings
    assert all(effects for _, effects in policy.judged)


def test_user_input_produces_call_model_effect() -> None:
    policy = SimPolicy()
    result = start_turn(policy)
    assert kinds(result.events) == ["UserSubmitted", "TurnStarted"]
    assert len(result.effects) == 1
    eff = result.effects[0]
    assert isinstance(eff, CallModel)
    assert eff.purpose == "turn"
    assert eff.request.purpose == "turn"
    assert eff.request.messages == result.state.messages
    assert result.state.in_turn


# --- judge paths: deny / ask, table-driven --------------------------------------


@pytest.mark.parametrize(
    ("verdicts", "answers", "expect_error_ids", "expect_ok_ids"),
    [
        pytest.param({}, {}, [], ["c1", "c2"], id="all-allow"),
        pytest.param({"c1": "deny"}, {}, ["c1"], ["c2"], id="deny-one"),
        pytest.param({"c1": "deny", "c2": "deny"}, {}, ["c1", "c2"], [], id="deny-all"),
        pytest.param({"c1": "ask"}, {"c1": True}, [], ["c1", "c2"], id="ask-allowed"),
        pytest.param({"c1": "ask"}, {"c1": False}, ["c1"], ["c2"], id="ask-denied"),
        pytest.param(
            {"c1": "ask", "c2": "deny"}, {"c1": False}, ["c1", "c2"], [], id="ask+deny"
        ),
    ],
)
def test_judge_paths(verdicts, answers, expect_error_ids, expect_ok_ids) -> None:
    policy = SimPolicy(verdicts=verdicts)
    script = Script(
        outputs=deque([mk_out(C1, C2), mk_out(TextBlock("done"))]),
        answers=answers,
    )
    run = simulate(policy, script)

    assert run.finishes == [TurnResult("ok", "done")]
    results_msg = next(m for m in run.state.messages if isinstance(m, ToolResultMessage))
    by_id = {r.call_id: r for r in results_msg.results}
    assert [r.call_id for r in results_msg.results] == ["c1", "c2"]  # declaration order
    for cid in expect_error_ids:
        assert by_id[cid].is_error, cid
        assert "permission denied" in by_id[cid].content
    for cid in expect_ok_ids:
        assert not by_id[cid].is_error, cid
    assert run.state.pending_permissions == ()
    assert_matched_pairs(run.state)

    decided = [e for e in run.events if isinstance(e, PermissionDecided)]
    asked = [e for e in run.events if isinstance(e, PermissionAsked)]
    ask_ids = {cid for cid, v in verdicts.items() if v == "ask"}
    deny_ids = {cid for cid, v in verdicts.items() if v == "deny"}
    assert {e.question.call_id for e in asked} == ask_ids
    assert {(e.call_id, e.source) for e in decided} == (
        {(cid, "policy") for cid in deny_ids} | {(cid, "user") for cid in ask_ids}
    )


def test_deny_synthesizes_error_result_at_judge_time() -> None:
    policy = SimPolicy(verdicts={"c1": "deny"})
    s1 = start_turn(policy)
    out = mk_out(C1, C2)
    s2 = step(s1.state, ModelResponded(out, s1.effects[0].request), policy)

    assert kinds(s2.events) == [
        "AssistantTurn",
        "ToolDeclared",
        "ToolDeclared",
        "PermissionDecided",
        "ToolFinished",
        "ToolStarted",
    ]
    decided = next(e for e in s2.events if isinstance(e, PermissionDecided))
    assert (decided.allowed, decided.source) == (False, "policy")
    synthesized = next(e for e in s2.events if isinstance(e, ToolFinished))
    assert synthesized.result.is_error and synthesized.result.call_id == "c1"
    # only c2 runs; no effect for the denied call
    assert s2.effects == (RunTools((C2,)),)
    assert {c.id for c in s2.state.pending_calls} == {"c2"}


def test_ask_pauses_only_that_call() -> None:
    policy = SimPolicy(verdicts={"c2": "ask"})
    s1 = start_turn(policy)
    s2 = step(s1.state, ModelResponded(mk_out(C1, C2), s1.effects[0].request), policy)

    assert s2.effects == (AskUser(PermissionAsked_q := s2.state.pending_permissions[0]), RunTools((C1,)))
    assert PermissionAsked_q.call_id == "c2"
    # c1 finishes while the ask is pending: no flush yet
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "ok")), policy)
    assert s3.effects == ()
    assert s3.state.pending_calls == (C2,)
    # the answer resolves it: PermissionDecided(user) then the call runs
    s4 = step(s3.state, PermissionAnswer("c2", allow=True), policy)
    assert kinds(s4.events) == ["PermissionDecided", "ToolStarted"]
    assert s4.effects == (RunTools((C2,)),)
    s5 = step(s4.state, ToolOutcome(ToolResult("c2", "ok2")), policy)
    assert kinds(s5.events) == ["ToolFinished"]
    assert isinstance(s5.effects[0], CallModel)
    assert isinstance(s5.state.messages[-1], ToolResultMessage)


# --- cancel ---------------------------------------------------------------------


def test_cancel_mid_batch_synthesizes_placeholders() -> None:
    policy = SimPolicy()
    s1 = start_turn(policy)
    s2 = step(s1.state, ModelResponded(mk_out(C1, C2), s1.effects[0].request), policy)
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "done")), policy)
    s4 = step(s3.state, Cancelled(), policy)

    assert kinds(s4.events) == ["ToolFinished", "TurnFinished"]
    placeholder = s4.events[0].result
    assert placeholder == ToolResult("c2", "cancelled", is_error=True)
    assert s4.effects == (Finish(TurnResult("aborted")),)
    final = s4.state
    assert not final.in_turn and not final.finished
    assert final.messages[-1] == ToolResultMessage(
        (ToolResult("c1", "done"), placeholder)
    )
    assert_matched_pairs(final)


def test_cancel_resolves_pending_permissions() -> None:
    policy = SimPolicy(verdicts={"c1": "ask"})
    s1 = start_turn(policy)
    s2 = step(s1.state, ModelResponded(mk_out(C1), s1.effects[0].request), policy)
    s3 = step(s2.state, Cancelled(), policy)

    assert kinds(s3.events) == ["PermissionDecided", "ToolFinished", "TurnFinished"]
    decided = s3.events[0]
    assert (decided.allowed, decided.source, decided.reason) == (False, "policy", "cancelled")
    assert s3.state.pending_permissions == ()
    assert s3.events[1].result.is_error


def test_cancel_while_idle_ends_session() -> None:
    policy = SimPolicy()
    result = step(SessionState(), Cancelled(), policy)
    assert kinds(result.events) == ["SessionEnded"]
    assert result.state.finished
    assert result.effects == ()
    # a finished session ignores everything
    again = step(result.state, UserInput("hello?"), policy)
    assert again == Step(result.state, (), ())


# --- max-turns -------------------------------------------------------------------


def test_max_turns_finishes_turn() -> None:
    policy = SimPolicy(max_turns=1)
    script = Script(outputs=deque([mk_out(C1)]))
    run = simulate(policy, script)

    assert run.finishes == [TurnResult("max_turns")]
    turn_finished = next(e for e in run.events if isinstance(e, TurnFinished))
    assert turn_finished.outcome == "max_turns"
    assert not run.state.in_turn
    assert_matched_pairs(run.state)


# --- truncation -------------------------------------------------------------------


def test_tool_result_truncated_to_cap_bytes() -> None:
    policy = SimPolicy(result_cap_bytes=16)
    s1 = start_turn(policy)
    s2 = step(s1.state, ModelResponded(mk_out(C1), s1.effects[0].request), policy)
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "x" * 100)), policy)

    finished = next(e for e in s3.events if isinstance(e, ToolFinished))
    assert finished.result.content == "x" * 16 + TRUNCATION_MARKER
    # the truncated result, not the original, is what enters the transcript
    assert s3.state.messages[-1].results[0].content == "x" * 16 + TRUNCATION_MARKER


def test_small_results_pass_untouched() -> None:
    policy = SimPolicy(result_cap_bytes=16)
    s1 = start_turn(policy)
    s2 = step(s1.state, ModelResponded(mk_out(C1), s1.effects[0].request), policy)
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "tiny")), policy)
    assert s3.state.messages[-1].results[0].content == "tiny"


# --- compaction -------------------------------------------------------------------


def test_compaction_transition() -> None:
    policy = SimPolicy(
        compact_when=lambda s: s.summary is None and len(s.messages) >= 3
    )
    s1 = start_turn(policy)
    s2 = step(s1.state, ModelResponded(mk_out(C1), s1.effects[0].request), policy)
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "data")), policy)

    # pressure crossed: the kernel asks for a compaction, not a turn
    assert len(s3.effects) == 1
    call = s3.effects[0]
    assert isinstance(call, CallModel) and call.purpose == "compaction"
    assert call.request.purpose == "compaction"

    summary_out = mk_out(TextBlock("summary of history"), usage=Usage(5, 3))
    s4 = step(s3.state, ModelResponded(summary_out, call.request), policy)

    assert kinds(s4.events) == ["Compacted"]
    compacted = s4.events[0]
    assert compacted == Compacted(
        summary="summary of history", first_kept_seq=0, usage=Usage(5, 3)
    )
    assert s4.state.summary == "summary of history"
    assert s4.state.messages == ()  # the old window is replaced by the summary
    # compaction usage still counts
    assert s4.state.turn_usage == s3.state.turn_usage + Usage(5, 3)
    # the deferred turn call resumes the loop
    nxt = s4.effects[0]
    assert isinstance(nxt, CallModel) and nxt.purpose == "turn"

    s5 = step(s4.state, ModelResponded(mk_out(TextBlock("done")), nxt.request), policy)
    assert kinds(s5.events) == ["AssistantTurn", "TurnFinished"]
    assert s5.effects == (Finish(TurnResult("ok", "done")),)


# --- defensive purity --------------------------------------------------------------


def test_stale_inputs_are_ignored() -> None:
    policy = SimPolicy()
    s1 = start_turn(policy)
    # user input mid-turn
    assert step(s1.state, UserInput("again"), policy) == Step(s1.state, (), ())
    # outcome for an unknown call
    assert step(s1.state, ToolOutcome(ToolResult("ghost", "x")), policy) == Step(
        s1.state, (), ()
    )
    # answer with no pending question
    assert step(s1.state, PermissionAnswer("ghost", True), policy) == Step(
        s1.state, (), ()
    )


def test_duplicate_tool_call_ids_deduped_not_wedged() -> None:
    # Two ToolCalls sharing an id (unvalidated provider passthrough) used to
    # make declared=2/results=1 forever: the turn never finished. The dupe is
    # dropped before any event is emitted, so one result completes the batch.
    policy = SimPolicy()
    s1 = start_turn(policy)
    call_model = s1.effects[0]
    assert isinstance(call_model, CallModel)
    dup = ToolCall("c1", "read", {"path": "b.py"})  # same id as C1
    s2 = step(
        s1.state,
        ModelResponded(mk_out(TextBlock("go"), C1, dup), call_model.request),
        policy,
    )
    assert kinds(s2.events) == ["AssistantTurn", "ToolDeclared", "ToolStarted"]
    assert [c.id for c in s2.state.declared] == ["c1"]
    # the surviving call is the FIRST occurrence
    assert s2.state.declared[0].args == {"path": "a.py"}
    # one result completes the batch and the turn continues
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "alpha")), policy)
    assert any(isinstance(e, CallModel) for e in s3.effects)
    assert_matched_pairs(s3.state)



def test_empty_compaction_keeps_window() -> None:
    policy = SimPolicy(
        compact_when=lambda s: s.summary is None and len(s.messages) >= 3
    )
    s1 = start_turn(policy)
    s2 = step(s1.state, ModelResponded(mk_out(C1), s1.effects[0].request), policy)
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "data")), policy)
    call = s3.effects[0]
    kept = s3.state.messages
    empty = mk_out(usage=Usage(5, 3))
    s4 = step(s3.state, ModelResponded(empty, call.request), policy)
    assert not any(isinstance(e, Compacted) for e in s4.events)
    assert s4.state.messages == kept
    assert s4.state.summary is None
    nxt = s4.effects[0]
    assert isinstance(nxt, CallModel) and nxt.purpose == "turn"
