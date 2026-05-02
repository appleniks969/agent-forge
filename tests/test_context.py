"""Tests for context.py — pressure tiers, P4 eviction, ContextWindow rotation,
manage_pressure() with and without a CompactionPort, session-resume bucketing."""
from __future__ import annotations

import pytest

from agent_forge.context import (
    ABSOLUTE_AGG, ABSOLUTE_P3, ABSOLUTE_P4,
    CompactionPort, CompactionResult, ContextBudget, ContextWindow, PressureTier,
    StratifiedWindowStrategy, _P4_NOTICE,
    assess_pressure, default_budget, estimate_tokens, estimate_tokens_list,
    evict_p4,
)
from agent_forge.messages import (
    AssistantMessage, ImageContent, TextContent, ThinkingContent,
    ToolCallContent, ToolResultMessage, UserMessage,
)
from agent_forge.models import DEFAULT_MODEL, Model, ModelCost

# ── Helpers ──────────────────────────────────────────────────────────────────


def _model(window: int = 1_000_000) -> Model:
    """Build a Model with a custom context window (everything else fixed)."""
    return Model(
        id=f"test-{window}", context_window=window, max_tokens=64_000,
        reasoning=False, cost=ModelCost(input=1, output=1, cache_read=0.1, cache_write=1),
    )


def _ucall(call_id: str, name: str, args: dict | None = None):
    """Convenience: build a ToolCallContent."""
    return ToolCallContent(id=call_id, name=name, arguments=args or {})


# ── assess_pressure: tier boundaries ─────────────────────────────────────────


class TestAssessPressure:
    def test_under_p4_returns_none(self):
        assert assess_pressure(10_000, _model()) == PressureTier.NONE

    def test_absolute_p4_threshold(self):
        # Just over absolute P4 → P4
        assert assess_pressure(ABSOLUTE_P4 + 1, _model()) == PressureTier.P4

    def test_absolute_p3_threshold(self):
        assert assess_pressure(ABSOLUTE_P3 + 1, _model()) == PressureTier.P3

    def test_absolute_agg_threshold(self):
        assert assess_pressure(ABSOLUTE_AGG + 1, _model()) == PressureTier.AGG

    def test_ratio_p4_for_small_window_model(self):
        """Even at low absolute counts, ratio > 0.85 trips P4."""
        small = _model(window=10_000)  # 8_500 = 85% threshold
        # 8_501 tokens / 10_000 window = 85.01% → P4
        assert assess_pressure(8_501, small) == PressureTier.P4

    def test_ratio_p3(self):
        small = _model(window=10_000)
        assert assess_pressure(9_001, small) == PressureTier.P3

    def test_ratio_agg(self):
        small = _model(window=10_000)
        assert assess_pressure(9_501, small) == PressureTier.AGG

    def test_zero_window_no_division_error(self):
        broken = Model(id="b", context_window=0, max_tokens=1, reasoning=False,
                       cost=ModelCost(0, 0, 0, 0))
        assert assess_pressure(1, broken) == PressureTier.NONE

    def test_p3_dominates_when_both_absolute_and_ratio_match(self):
        """If a value qualifies for AGG, it should not regress to P3."""
        assert assess_pressure(ABSOLUTE_AGG + 1, _model()) == PressureTier.AGG


# ── estimate_tokens ──────────────────────────────────────────────────────────


class TestEstimateTokens:
    def test_user_string_content(self):
        # 8 chars / 4 = 2 tokens
        assert estimate_tokens(UserMessage(content="abcdefgh")) == 2

    def test_user_structured_content(self):
        # 8 chars total / 4 = 2 tokens
        msg = UserMessage(content=(TextContent(text="abcd"), TextContent(text="efgh")))
        assert estimate_tokens(msg) == 2

    def test_assistant_text_block(self):
        msg = AssistantMessage(content=(TextContent(text="hello world"),))
        assert estimate_tokens(msg) == len("hello world") // 4

    def test_assistant_thinking_block_counted(self):
        msg = AssistantMessage(content=(ThinkingContent(thinking="ponder" * 10),))
        # Thinking text is counted under estimate_tokens (it goes back to API)
        assert estimate_tokens(msg) > 0

    def test_tool_result_string(self):
        msg = ToolResultMessage(tool_call_id="x", content="abcdefgh")
        assert estimate_tokens(msg) == 2

    def test_tool_result_structured_with_image(self):
        msg = ToolResultMessage(
            tool_call_id="x",
            content=(TextContent(text="ok"), ImageContent(media_type="image/png", data="b64")),
        )
        # "ok [image/image/png]" → 20 / 4 = 5
        assert estimate_tokens(msg) >= 4

    def test_estimate_list(self):
        msgs = [UserMessage(content="abcd"), UserMessage(content="efgh")]
        assert estimate_tokens_list(msgs) == 2


# ── default_budget ───────────────────────────────────────────────────────────


def test_default_budget_caps_keep_recent_at_40k():
    # 1M-window model: 10% = 100k → capped at 40k
    b = default_budget(_model(window=1_000_000))
    assert b.keep_recent_tokens == 40_000
    assert b.recency_turns == 10


def test_default_budget_uses_10pct_for_small_window():
    b = default_budget(_model(window=200_000))
    assert b.keep_recent_tokens == 20_000


# ── evict_p4 (free function) ─────────────────────────────────────────────────


class TestEvictP4:
    def test_passes_through_small_results(self):
        msgs = [ToolResultMessage(tool_call_id="x", content="hello")]
        out = evict_p4(msgs)
        assert out[0].content == "hello"

    def test_truncates_large_string_results(self):
        big = "A" * 2_000  # > 1024 byte threshold
        msgs = [ToolResultMessage(tool_call_id="x", content=big)]
        out = evict_p4(msgs)
        assert out[0].content == _P4_NOTICE
        # Identity preserved for re-correlation:
        assert out[0].tool_call_id == "x"

    def test_preserves_is_error_flag(self):
        big = "A" * 2_000
        msgs = [ToolResultMessage(tool_call_id="x", content=big, is_error=True)]
        out = evict_p4(msgs)
        assert out[0].is_error is True

    def test_does_not_touch_non_tool_messages(self):
        msgs = [
            UserMessage(content="A" * 5_000),
            AssistantMessage(content=(TextContent(text="A" * 5_000),)),
        ]
        out = evict_p4(msgs)
        assert out[0].content == "A" * 5_000  # user untouched
        assert out[1].content[0].text == "A" * 5_000  # assistant untouched

    def test_does_not_touch_structured_tool_results(self):
        """Structured content (with images) is not truncated — the heuristic
        only handles string content."""
        msg = ToolResultMessage(
            tool_call_id="x",
            content=(TextContent(text="A" * 5_000),),
        )
        out = evict_p4([msg])
        assert isinstance(out[0].content, tuple)


# ── StratifiedWindowStrategy ─────────────────────────────────────────────────


class TestStratifiedWindowStrategy:
    def test_build_without_action_log_no_prefix(self):
        strat = StratifiedWindowStrategy()
        msgs = strat.build([], [], UserMessage(content="hi"))
        assert len(msgs) == 1
        assert isinstance(msgs[0], UserMessage)

    def test_build_with_action_log_inserts_prefix_pair(self):
        from agent_forge.context import ActionLogEntry
        strat = StratifiedWindowStrategy()
        log = [ActionLogEntry(turn=1, summary="[T1] did stuff", tokens=5)]
        msgs = strat.build(log, [], UserMessage(content="next"))
        # Prefix is User+Assistant pair, then current user
        assert len(msgs) == 3
        assert isinstance(msgs[0], UserMessage)
        assert "[Prior session actions]" in msgs[0].content
        assert "[T1]" in msgs[0].content
        assert isinstance(msgs[1], AssistantMessage)
        assert isinstance(msgs[2], UserMessage)

    def test_build_strips_thinking_from_old_turns_keeps_in_newest(self):
        from agent_forge.context import TurnRecord
        strat = StratifiedWindowStrategy()
        old = TurnRecord(
            turn=1, user_message=UserMessage(content="old"),
            assistant_messages=[AssistantMessage(content=(
                ThinkingContent(thinking="old thoughts"),
                TextContent(text="old reply"),
            ))],
            tool_calls=[], tokens=10,
        )
        new = TurnRecord(
            turn=2, user_message=UserMessage(content="new"),
            assistant_messages=[AssistantMessage(content=(
                ThinkingContent(thinking="new thoughts"),
                TextContent(text="new reply"),
            ))],
            tool_calls=[], tokens=10,
        )
        msgs = strat.build([], [old, new], UserMessage(content="now"))
        # Old assistant: thinking stripped
        old_asst = msgs[1]
        assert all(not isinstance(b, ThinkingContent) for b in old_asst.content)
        # New assistant: thinking preserved
        new_asst = msgs[3]
        assert any(isinstance(b, ThinkingContent) for b in new_asst.content)

    def test_summarise_turn_includes_tool_calls(self):
        from agent_forge.context import TurnRecord
        from dataclasses import dataclass

        @dataclass
        class _FakeTC:
            name: str
            args: dict
            result: object = None
        strat = StratifiedWindowStrategy()
        rec = TurnRecord(
            turn=3, user_message=UserMessage(content="please read the file"),
            assistant_messages=[],
            tool_calls=[_FakeTC(name="Read", args={"path": "x.txt"})],
            tokens=5,
        )
        entry = strat.summarise_turn(rec)
        assert entry.turn == 3
        assert "[T3]" in entry.summary
        assert "Read x.txt" in entry.summary

    def test_summarise_turn_with_no_tool_calls(self):
        from agent_forge.context import TurnRecord
        strat = StratifiedWindowStrategy()
        rec = TurnRecord(
            turn=1, user_message=UserMessage(content="just chatting"),
            assistant_messages=[], tool_calls=[], tokens=3,
        )
        entry = strat.summarise_turn(rec)
        assert "[T1]" in entry.summary
        assert "just chatting" in entry.summary


# ── ContextWindow.receive(): rotation + ActionLog ────────────────────────────


class TestContextWindowReceive:
    def test_first_turn_no_eviction(self):
        cw = ContextWindow(_model())
        cw.receive(UserMessage(content="hi"), [], [])
        assert cw.current_turn == 1
        # access internals via build_messages — there should be one turn in window
        msgs = cw.build_messages(UserMessage(content="next"))
        # No action-log prefix: just (recent_turn user) + current user = 2 msgs
        assert len(msgs) == 2

    def test_evicts_to_action_log_when_recency_turns_exceeded(self):
        # Tiny budget: only 2 turns kept
        budget = ContextBudget(keep_recent_tokens=10_000_000, recency_turns=2)
        cw = ContextWindow(_model(), budget=budget)
        for i in range(5):
            cw.receive(UserMessage(content=f"turn {i}"), [], [])
        # 5 turns received, only 2 should remain in recency
        msgs = cw.build_messages(UserMessage(content="now"))
        # Action-log prefix (User+Assistant pair) + 2 recent users + current = 5
        assert len(msgs) == 5
        assert "[Prior session actions]" in msgs[0].content
        # 3 turns should have been logged
        assert msgs[0].content.count("[T") == 3

    def test_always_keeps_at_least_one_turn_even_if_token_budget_exceeded(self):
        budget = ContextBudget(keep_recent_tokens=1, recency_turns=10)  # impossibly tight
        cw = ContextWindow(_model(), budget=budget)
        cw.receive(UserMessage(content="A" * 1000), [], [])
        # The single remaining turn cannot be evicted (invariant: keep ≥ 1)
        msgs = cw.build_messages(UserMessage(content="next"))
        assert len(msgs) == 2  # one recent + current

    def test_receive_resets_synced_total(self):
        cw = ContextWindow(_model())
        cw.sync_total_tokens(50_000)
        assert cw.estimate_tokens() == 50_000
        cw.receive(UserMessage(content="x"), [], [])
        # After receive, the synced total is invalidated → falls back to heuristic
        assert cw.estimate_tokens() != 50_000


# ── ContextWindow.estimate_tokens / sync_total_tokens ────────────────────────


def test_estimate_uses_real_count_when_synced():
    cw = ContextWindow(_model())
    cw.sync_total_tokens(123_456)
    assert cw.estimate_tokens() == 123_456


def test_estimate_falls_back_to_heuristic_when_unsynced():
    cw = ContextWindow(_model())
    cw.receive(UserMessage(content="A" * 400), [], [])
    # Heuristic: 400/4 = 100 tokens, no action-log overhead
    assert cw.estimate_tokens() == 100


# ── apply_eviction (P4 in-place) ─────────────────────────────────────────────


class TestApplyEviction:
    def test_truncates_old_tool_results_keeps_newest_intact(self):
        cw = ContextWindow(_model())
        big = "A" * 2_000
        # Old turn: a big tool result
        cw.receive(
            UserMessage(content="old"),
            [ToolResultMessage(tool_call_id="t1", content=big)],
            [],
        )
        # Newest turn: another big tool result
        cw.receive(
            UserMessage(content="new"),
            [ToolResultMessage(tool_call_id="t2", content=big)],
            [],
        )
        cw.apply_eviction()
        msgs = cw.build_messages(UserMessage(content="now"))
        # Find old/new tool results in built messages
        tool_results = [m for m in msgs if isinstance(m, ToolResultMessage)]
        assert len(tool_results) == 2
        # Old (t1) should be evicted, new (t2) intact
        old_tr = next(m for m in tool_results if m.tool_call_id == "t1")
        new_tr = next(m for m in tool_results if m.tool_call_id == "t2")
        assert old_tr.content == _P4_NOTICE
        assert new_tr.content == big

    def test_apply_eviction_resets_synced_total(self):
        cw = ContextWindow(_model())
        cw.sync_total_tokens(99_999)
        cw.apply_eviction()
        # After eviction the synced total is stale; estimate should not return it
        assert cw.estimate_tokens() != 99_999


# ── manage_pressure: the P3/AGG/P4 dispatch ──────────────────────────────────


class _RecordingPort(CompactionPort):
    def __init__(self):
        self.calls: list[int] = []

    async def compact(self, messages, keep_recent_turns):
        self.calls.append(keep_recent_turns)
        # Return one synthetic user turn to keep init_from_existing happy
        return CompactionResult(
            messages=[UserMessage(content="[compacted]")],
            summary="compacted",
        )


class _BrokenPort(CompactionPort):
    async def compact(self, messages, keep_recent_turns):
        raise RuntimeError("port crashed")


class TestManagePressure:
    @pytest.mark.asyncio
    async def test_none_does_nothing(self):
        cw = ContextWindow(_model())
        cw.receive(UserMessage(content="hi"), [], [])
        tier = await cw.manage_pressure()
        assert tier == PressureTier.NONE

    @pytest.mark.asyncio
    async def test_p4_falls_through_to_apply_eviction(self):
        cw = ContextWindow(_model())
        # Force P4 by syncing a token count above the threshold
        cw.receive(UserMessage(content="x"), [], [])
        cw.sync_total_tokens(ABSOLUTE_P4 + 100)
        tier = await cw.manage_pressure()
        assert tier == PressureTier.P4

    @pytest.mark.asyncio
    async def test_p3_calls_compaction_port_with_keep_recent_6(self):
        port = _RecordingPort()
        cw = ContextWindow(_model(), compaction_port=port)
        cw.receive(UserMessage(content="x"), [], [])
        cw.sync_total_tokens(ABSOLUTE_P3 + 100)
        tier = await cw.manage_pressure()
        assert tier == PressureTier.P3
        assert port.calls == [6]

    @pytest.mark.asyncio
    async def test_agg_calls_compaction_port_with_keep_recent_2(self):
        port = _RecordingPort()
        cw = ContextWindow(_model(), compaction_port=port)
        cw.receive(UserMessage(content="x"), [], [])
        cw.sync_total_tokens(ABSOLUTE_AGG + 100)
        tier = await cw.manage_pressure()
        assert tier == PressureTier.AGG
        assert port.calls == [2]

    @pytest.mark.asyncio
    async def test_p3_falls_back_to_p4_when_no_port(self):
        cw = ContextWindow(_model())  # no port
        cw.receive(
            UserMessage(content="x"),
            [ToolResultMessage(tool_call_id="t", content="A" * 5_000)],
            [],
        )
        # Need at least 2 turns for apply_eviction to actually rewrite anything
        cw.receive(
            UserMessage(content="y"),
            [ToolResultMessage(tool_call_id="t2", content="B" * 5_000)],
            [],
        )
        cw.sync_total_tokens(ABSOLUTE_P3 + 100)
        tier = await cw.manage_pressure()
        assert tier == PressureTier.P3
        # Old turn should now be the P4 notice
        msgs = cw.build_messages(UserMessage(content="z"))
        truncated = [m for m in msgs if isinstance(m, ToolResultMessage) and m.content == _P4_NOTICE]
        assert len(truncated) >= 1

    @pytest.mark.asyncio
    async def test_compaction_port_failure_falls_back_to_p4(self):
        cw = ContextWindow(_model(), compaction_port=_BrokenPort())
        cw.receive(
            UserMessage(content="x"),
            [ToolResultMessage(tool_call_id="t", content="A" * 5_000)],
            [],
        )
        cw.receive(
            UserMessage(content="y"),
            [ToolResultMessage(tool_call_id="t2", content="B" * 5_000)],
            [],
        )
        cw.sync_total_tokens(ABSOLUTE_AGG + 100)
        tier = await cw.manage_pressure()
        # Tier was AGG when assessed; fallback to P4 happened silently
        assert tier == PressureTier.AGG
        msgs = cw.build_messages(UserMessage(content="z"))
        truncated = [m for m in msgs if isinstance(m, ToolResultMessage) and m.content == _P4_NOTICE]
        assert len(truncated) >= 1


# ── Session resume: init_from_existing token-bucketing ───────────────────────


class TestInitFromExisting:
    def test_short_history_all_in_recency(self):
        cw = ContextWindow(_model())
        msgs = [
            UserMessage(content="t1"),
            AssistantMessage(content=(TextContent(text="r1"),)),
            UserMessage(content="t2"),
            AssistantMessage(content=(TextContent(text="r2"),)),
        ]
        cw.init_from_existing(msgs)
        assert cw.current_turn == 2
        # No action log: short history fits in recency
        rebuilt = cw.build_messages(UserMessage(content="t3"))
        assert "[Prior session actions]" not in str(getattr(rebuilt[0], "content", ""))

    def test_long_history_oldest_become_action_log(self):
        # Tiny recency cap forces older turns into the log
        budget = ContextBudget(keep_recent_tokens=10_000_000, recency_turns=2)
        cw = ContextWindow(_model(), budget=budget)
        msgs = []
        for i in range(5):
            msgs.append(UserMessage(content=f"prompt {i}"))
            msgs.append(AssistantMessage(content=(TextContent(text=f"reply {i}"),)))
        cw.init_from_existing(msgs)
        # Last 2 turns kept; 3 oldest in action log
        assert cw.current_turn == 2  # current_turn counts only recent
        rebuilt = cw.build_messages(UserMessage(content="now"))
        assert "[Prior session actions]" in rebuilt[0].content
        # 3 logged turns
        assert rebuilt[0].content.count("[T") == 3

    def test_empty_messages_no_op(self):
        cw = ContextWindow(_model())
        cw.init_from_existing([])
        assert cw.current_turn == 0


# ── clear() resets everything ────────────────────────────────────────────────


def test_clear_resets_state():
    cw = ContextWindow(_model())
    cw.receive(UserMessage(content="x"), [], [])
    cw.sync_total_tokens(100_000)
    cw.clear()
    assert cw.current_turn == 0
    assert cw.estimate_tokens() == 0
    msgs = cw.build_messages(UserMessage(content="now"))
    assert len(msgs) == 1  # just the current user message
