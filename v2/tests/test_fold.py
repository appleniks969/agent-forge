"""The load-bearing property: fold(events emitted by step) == live state.

Resume and live are the same function. Scenarios end at turn boundaries
(TurnFinished / SessionEnded), which is where the equality is guaranteed.
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
    to_envelopes,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from forge.kernel.state import SessionState, fold
from forge.kernel.step import Cancelled, ModelResponded, ToolOutcome, UserInput, step
from forge.kernel.types import TextBlock, ToolCall, ToolResult, Usage


def C(i: int) -> ToolCall:
    return ToolCall(f"c{i}", "read", {"path": f"f{i}.py"})


SCENARIOS = {
    "plain-answer": dict(
        policy=lambda: SimPolicy(),
        outputs=lambda: [mk_out(TextBlock("hi"))],
    ),
    "tools-all-allow": dict(
        policy=lambda: SimPolicy(),
        outputs=lambda: [mk_out(TextBlock("w"), C(1), C(2)), mk_out(TextBlock("done"))],
    ),
    "deny-and-ask": dict(
        policy=lambda: SimPolicy(verdicts={"c1": "deny", "c2": "ask"}),
        outputs=lambda: [mk_out(C(1), C(2), C(3)), mk_out(TextBlock("done"))],
        answers={"c2": False},
    ),
    "max-turns": dict(
        policy=lambda: SimPolicy(max_turns=1),
        outputs=lambda: [mk_out(C(1))],
    ),
    "compaction": dict(
        policy=lambda: SimPolicy(
            compact_when=lambda s: s.summary is None and len(s.messages) >= 3
        ),
        outputs=lambda: [
            mk_out(C(1)),
            mk_out(TextBlock("the summary"), usage=Usage(5, 1)),
            mk_out(TextBlock("done")),
        ],
    ),
    "two-turns": dict(
        policy=lambda: SimPolicy(),
        outputs=lambda: [
            mk_out(TextBlock("a"), C(1)),
            mk_out(TextBlock("first done")),
            mk_out(TextBlock("second done")),
        ],
        user_texts=("one", "two"),
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_fold_reproduces_live_state(name: str) -> None:
    spec = SCENARIOS[name]
    run = simulate(
        spec["policy"](),
        Script(outputs=deque(spec["outputs"]()), answers=dict(spec.get("answers", {}))),
        user_texts=spec.get("user_texts", ("do the thing",)),
    )
    assert run.finishes, "scenario must reach a turn boundary"
    folded = fold(to_envelopes(run.events))
    assert folded == run.state
    assert_matched_pairs(folded)


def test_fold_reproduces_cancelled_turn() -> None:
    policy = SimPolicy(verdicts={"c2": "ask"})
    events = []
    s1 = step(SessionState(), UserInput("go"), policy)
    events += s1.events
    s2 = step(s1.state, ModelResponded(mk_out(C(1), C(2)), s1.effects[0].request), policy)
    events += s2.events
    s3 = step(s2.state, ToolOutcome(ToolResult("c1", "partial")), policy)
    events += s3.events
    s4 = step(s3.state, Cancelled(), policy)
    events += s4.events
    s5 = step(s4.state, Cancelled(), policy)  # idle now: ends the session
    events += s5.events

    assert kinds(events)[-1] == "SessionEnded"
    folded = fold(to_envelopes(events))
    assert folded == s5.state
    assert folded.finished
    assert_matched_pairs(folded)


def test_fold_skips_transient_envelopes() -> None:
    from forge.kernel.events import TextDelta, make_envelope

    policy = SimPolicy()
    run = simulate(policy, Script(outputs=deque([mk_out(TextBlock("hi"))])))
    envs = list(to_envelopes(run.events))
    envs.insert(2, make_envelope(seq=99, sid="s1", body=TextDelta("h")))
    assert fold(envs) == run.state


# --- randomized interleavings ----------------------------------------------------

VERDICT = st.sampled_from(["allow", "deny", "ask"])


@settings(max_examples=200, deadline=None)
@given(data=st.data())
def test_fold_equals_live_under_random_interleavings(data: st.DataObject) -> None:
    n_calls = data.draw(st.integers(min_value=0, max_value=3), label="n_calls")
    calls = tuple(C(i) for i in range(n_calls))
    verdicts = {
        call.id: data.draw(VERDICT, label=f"verdict[{call.id}]") for call in calls
    }
    answers = {
        cid: data.draw(st.booleans(), label=f"answer[{cid}]")
        for cid, v in verdicts.items()
        if v == "ask"
    }
    policy = SimPolicy(verdicts=verdicts)
    script = Script(
        outputs=deque(
            [
                mk_out(TextBlock("working"), *calls, usage=Usage(11, 3)),
                mk_out(TextBlock("done"), usage=Usage(7, 2)),
            ]
        ),
        answers=answers,
    )

    def pick(n: int) -> int:
        return data.draw(st.integers(min_value=0, max_value=n - 1), label="pick")

    run = simulate(policy, script, pick=pick)

    assert run.finishes and run.finishes[-1].outcome == "ok"
    final = run.state
    assert not final.in_turn
    assert final.declared == () and final.results == ()
    assert final.pending_permissions == ()
    assert_matched_pairs(final)

    folded = fold(to_envelopes(run.events))
    assert folded == final
