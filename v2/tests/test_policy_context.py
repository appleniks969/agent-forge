"""Context policy: estimation/calibration, pressure tiers, window selection,
should_compact, and the StandardPolicy composition."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from forge.kernel.state import SessionState
from forge.kernel.types import (
    AssistantMessage,
    Effects,
    Message,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)
from forge.policy import COMPACTION_INSTRUCTION, StandardPolicy
from forge.policy.context import (
    EVICTED_NOTICE,
    ContextBudget,
    PressureTier,
    TokenEstimator,
    assess_pressure,
    default_budget,
    select_window,
    should_compact,
)

WS = PurePosixPath("/ws")


def turn(i: int, *, result: str = "", is_error: bool = False) -> tuple[Message, ...]:
    call = ToolCall(id=f"t{i}", name="read", args={"path": f"src/f{i}.py"})
    return (
        UserMessage(f"task {i}"),
        AssistantMessage((TextBlock(f"working {i}"), call)),
        ToolResultMessage((ToolResult(f"t{i}", result or f"contents {i}", is_error),)),
        AssistantMessage((TextBlock(f"done {i}"),)),
    )


# --- token estimation --------------------------------------------------------


def test_text_estimate_is_chars_over_four() -> None:
    assert TokenEstimator().text("a" * 40) == 10


def test_message_estimates_cover_all_message_kinds() -> None:
    est = TokenEstimator()
    assert est.message(UserMessage("a" * 40)) == 10
    assert est.message(AssistantMessage((TextBlock("b" * 80),))) == 20
    assert est.message(ToolResultMessage((ToolResult("c1", "c" * 40),))) == 10
    # tool calls count name + args
    msg = AssistantMessage((ToolCall("c1", "read", {"path": "x" * 36}),))
    assert est.message(msg) > 0


def test_recalibration_scales_future_estimates() -> None:
    est = TokenEstimator()
    # window estimated at 100 tokens; the API reported 200 real context tokens
    calibrated = est.recalibrated(100, Usage(input_tokens=150, cache_read_tokens=50))
    assert calibrated.scale == pytest.approx(2.0)
    assert calibrated.text("a" * 40) == 20  # heuristic now corrected


def test_recalibration_is_clamped_and_zero_safe() -> None:
    est = TokenEstimator()
    assert est.recalibrated(10, Usage(input_tokens=100_000)).scale == 4.0
    assert est.recalibrated(100_000, Usage(input_tokens=1)).scale == 0.25
    assert est.recalibrated(0, Usage(input_tokens=100)) is est
    assert est.recalibrated(100, Usage()) is est


# --- pressure tiers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "tier"),
    [
        (10_000, PressureTier.NONE),
        (60_000, PressureTier.EVICT),
        (120_000, PressureTier.COMPACT),
        (250_000, PressureTier.CRITICAL),
    ],
)
def test_absolute_thresholds(tokens: int, tier: PressureTier) -> None:
    assert assess_pressure(tokens, ContextBudget(context_tokens=1_000_000)) == tier


@pytest.mark.parametrize(
    ("tokens", "tier"),
    [
        (800, PressureTier.NONE),
        (880, PressureTier.EVICT),
        (920, PressureTier.COMPACT),
        (990, PressureTier.CRITICAL),
    ],
)
def test_ratio_thresholds(tokens: int, tier: PressureTier) -> None:
    assert assess_pressure(tokens, ContextBudget(context_tokens=1_000)) == tier


def test_zero_window_means_no_ratio_pressure() -> None:
    assert assess_pressure(100, ContextBudget(context_tokens=0)) == PressureTier.NONE


# --- window selection ------------------------------------------------------------


def test_no_pressure_keeps_everything_verbatim() -> None:
    msgs = turn(1) + turn(2)
    window = select_window(msgs, budget=default_budget(200_000))
    assert window.messages == msgs
    assert window.action_log == ()
    assert window.pressure == PressureTier.NONE
    assert window.estimated_tokens > 0


def test_recency_cap_evicts_oldest_turns_to_action_log() -> None:
    msgs = turn(1) + turn(2) + turn(3)
    budget = ContextBudget(context_tokens=200_000, recency_turns=2)
    window = select_window(msgs, budget=budget)

    assert window.action_log == ("[T1] task 1 -> read(src/f1.py)",)
    head = window.messages[0]
    assert isinstance(head, UserMessage)
    assert "[Prior actions]" in head.text and "[T1] task 1" in head.text
    ack = window.messages[1]
    assert isinstance(ack, AssistantMessage)
    # kept turns survive whole and in order — matched pairs intact
    assert window.messages[2:] == turn(2) + turn(3)


def test_token_cap_evicts_but_newest_turn_always_survives() -> None:
    msgs = turn(1, result="x" * 4000) + turn(2, result="y" * 4000)
    budget = ContextBudget(context_tokens=1_000_000, keep_recent_tokens=100)
    window = select_window(msgs, budget=budget)
    assert len(window.action_log) == 1
    assert window.messages[2:] == turn(2, result="y" * 4000)


def test_action_log_marks_tool_errors() -> None:
    msgs = turn(1, is_error=True) + turn(2)
    budget = ContextBudget(context_tokens=200_000, recency_turns=1)
    window = select_window(msgs, budget=budget)
    assert window.action_log == ("[T1] task 1 -> read(src/f1.py)!",)


def test_summary_is_prepended() -> None:
    msgs = turn(1)
    window = select_window(msgs, summary="we fixed the parser", budget=default_budget(200_000))
    head = window.messages[0]
    assert isinstance(head, UserMessage)
    assert head.text.startswith("[Conversation summary]\nwe fixed the parser")
    assert window.messages[2:] == msgs  # ack pair then the turn


def test_post_compaction_empty_window_renders_summary_without_ack() -> None:
    window = select_window((), summary="the summary", budget=default_budget(200_000))
    assert len(window.messages) == 1
    assert isinstance(window.messages[0], UserMessage)
    assert "the summary" in window.messages[0].text


def test_userless_continuation_gets_no_ack_after_context_block() -> None:
    # post-compaction continuation: assistant output with no leading user turn
    msgs = (AssistantMessage((TextBlock("continuing"),)),)
    window = select_window(msgs, summary="prior work", budget=default_budget(200_000))
    assert isinstance(window.messages[0], UserMessage)  # summary block
    assert window.messages[1] == AssistantMessage((TextBlock("continuing"),))
    assert len(window.messages) == 2  # no synthetic ack in between


def test_pressure_evicts_big_results_in_old_turns_only() -> None:
    big = "z" * 8_000
    msgs = turn(1, result=big) + turn(2, result=big)
    budget = ContextBudget(
        context_tokens=1_000_000,
        keep_recent_tokens=1_000_000,
        evict_at=1_000,  # force EVICT pressure
        evict_result_bytes=1_024,
    )
    window = select_window(msgs, budget=budget)
    assert window.pressure != PressureTier.NONE
    old_results = [
        m for m in window.messages[:4] if isinstance(m, ToolResultMessage)
    ]
    new_results = [
        m for m in window.messages[4:] if isinstance(m, ToolResultMessage)
    ]
    assert old_results[0].results[0].content == EVICTED_NOTICE
    assert new_results[0].results[0].content == big  # newest turn untouched


# --- should_compact ---------------------------------------------------------------


def test_should_not_compact_when_truncation_relieves() -> None:
    big = "z" * 50_000
    msgs = turn(1, result=big) + turn(2)
    budget = ContextBudget(
        context_tokens=1_000_000,
        keep_recent_tokens=2_000,  # recency eviction drops the big turn
        evict_at=1_000,
        compact_at=3_000,
    )
    assert not should_compact(msgs, budget=budget)


def test_should_compact_when_even_truncated_window_stays_hot() -> None:
    # plain text pressure: nothing for result-eviction to truncate
    msgs = (
        UserMessage("task"),
        AssistantMessage((TextBlock("w" * 40_000),)),
    )
    budget = ContextBudget(
        context_tokens=1_000_000,
        keep_recent_tokens=1_000_000,
        evict_at=1_000,
        compact_at=3_000,
    )
    assert should_compact(msgs, budget=budget)


# --- StandardPolicy ------------------------------------------------------------------


READ_SPEC = ToolSpec(name="read", description="read a file", effects=Effects.READ_PATH)


def policy(**kwargs: object) -> StandardPolicy:
    defaults: dict = dict(
        model="m1", context_tokens=200_000, tools=(READ_SPEC,), ws_root=WS
    )
    defaults.update(kwargs)
    return StandardPolicy(**defaults)


def state_with(messages: tuple[Message, ...], summary: str | None = None) -> SessionState:
    return SessionState(messages=messages, summary=summary, in_turn=True)


def test_build_request_turn_carries_window_tools_and_system() -> None:
    p = policy()
    req = p.build_request(state_with(turn(1)), "turn")
    assert req.purpose == "turn"
    assert req.model == "m1"
    assert req.messages == turn(1)
    assert req.tools == (READ_SPEC,)
    names = [s.name for s in req.system]
    assert names == ["identity", "tools"]


def test_build_request_compaction_drops_tools_and_appends_instruction() -> None:
    p = policy()
    req = p.build_request(state_with(turn(1)), "compaction")
    assert req.purpose == "compaction"
    assert req.tools == ()
    last = req.messages[-1]
    assert isinstance(last, UserMessage)
    assert last.text == COMPACTION_INSTRUCTION


def test_policy_should_compact_uses_summary_and_budget() -> None:
    hot = ContextBudget(
        context_tokens=1_000_000,
        keep_recent_tokens=1_000_000,
        evict_at=100,
        compact_at=200,
    )
    msgs = (UserMessage("t"), AssistantMessage((TextBlock("w" * 10_000),)))
    assert policy(budget=hot).should_compact(state_with(msgs))
    assert not policy().should_compact(state_with(msgs))


def test_observe_usage_calibrates_the_estimator() -> None:
    p = policy()
    p.build_request(state_with(turn(1)), "turn")  # records the window estimate
    before = p.estimator.scale
    estimate = p.build_request(state_with(turn(1)), "turn").messages  # same window
    real = p.estimator.messages(estimate) * 3  # API says 3x the heuristic
    p.observe_usage(Usage(input_tokens=real))
    assert p.estimator.scale == pytest.approx(before * 3)


def test_tools_supplier_is_requeried_each_build() -> None:
    current: list[ToolSpec] = [READ_SPEC]
    p = policy(tools=lambda: tuple(current))
    assert p.build_request(state_with(turn(1)), "turn").tools == (READ_SPEC,)
    bash = ToolSpec(name="bash", description="run", effects=Effects.EXEC)
    current.append(bash)
    req = p.build_request(state_with(turn(1)), "turn")
    assert req.tools == (READ_SPEC, bash)
    tools_text = next(s.text for s in req.system if s.name == "tools")
    assert "bash" in tools_text
