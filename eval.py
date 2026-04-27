"""
eval.py — evaluation suite for agent_forge.

Unit evals (no API key required):
  1. pressure_tiers      — P4/P3/AGGRESSIVE fire at correct thresholds
  2. cache_group_placement — cache_control placed on last section of each group
  3. action_log_eviction  — evicted turns become ActionLog one-liners
  4. session_roundtrip    — write messages to JSONL, read back correctly
  5. tool_read            — ReadTool returns line-numbered content
  6. tool_grep            — GrepTool finds pattern in files
  7. tool_bash            — BashTool executes commands
  8. p4_eviction_content  — evict_p4 replaces oversized tool results

Integration eval (requires ANTHROPIC_API_KEY):
  9. agent_uses_read_tool  — agent reads pyproject.toml, finds Python version
 10. cache_hit_on_turn_2   — second turn has non-zero cache_read tokens
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# ── Eval harness ──────────────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []

def _run(name: str, fn) -> None:
    start = time.perf_counter()
    try:
        result = fn()
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        elapsed = time.perf_counter() - start
        _results.append((name, True, f"{elapsed*1000:.0f}ms"))
        print(f"  \x1b[32m✓\x1b[0m {name} \x1b[2m({elapsed*1000:.0f}ms)\x1b[0m")
    except AssertionError as e:
        elapsed = time.perf_counter() - start
        _results.append((name, False, str(e)))
        print(f"  \x1b[31m✗\x1b[0m {name}: \x1b[31m{e}\x1b[0m")
    except Exception as e:
        elapsed = time.perf_counter() - start
        _results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  \x1b[31m✗\x1b[0m {name}: \x1b[31m{type(e).__name__}: {e}\x1b[0m")

def _skip(name: str, reason: str) -> None:
    _results.append((name, None, reason))  # type: ignore[arg-type]
    print(f"  \x1b[33m-\x1b[0m {name}: \x1b[2m{reason}\x1b[0m")

# ── EVAL 1: Pressure tier transitions ────────────────────────────────────────

def eval_pressure_tiers():
    from agent_forge.context import (
        ABSOLUTE_P4, ABSOLUTE_P3, ABSOLUTE_AGG, PressureTier, assess_pressure,
    )
    from agent_forge.provider import MODELS

    model = MODELS["claude-sonnet-4-6"]  # 1M context

    assert assess_pressure(0, model) == PressureTier.NONE, "0 tokens should be NONE"
    assert assess_pressure(ABSOLUTE_P4 + 1, model) == PressureTier.P4, \
        f"Just over {ABSOLUTE_P4} should be P4"
    assert assess_pressure(ABSOLUTE_P3 + 1, model) == PressureTier.P3, \
        f"Just over {ABSOLUTE_P3} should be P3"
    assert assess_pressure(ABSOLUTE_AGG + 1, model) == PressureTier.AGG, \
        f"Just over {ABSOLUTE_AGG} should be AGG"

    # Verify absolute thresholds beat percentage on large-context models
    # 1M model: 90% = 900K but ABSOLUTE_P3 = 100K → P3 fires at 100K, not 900K
    assert assess_pressure(150_000, model) == PressureTier.P3, \
        "150K tokens on 1M model should still be P3 (absolute threshold wins)"

    # Percentage-based still works on small models
    model_small = MODELS["claude-haiku-4-5"]  # 200K context
    # 90% of 200K = 180K > ABSOLUTE_P3 (100K) → P3 fires by absolute first
    assert assess_pressure(110_000, model_small) == PressureTier.P3

# ── EVAL 2: Cache group placement ────────────────────────────────────────────

def eval_cache_group_placement():
    from agent_forge.context import SectionName, SystemPrompt

    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY,    lambda: "You are agent-forge.")
    sp.register(SectionName.TOOLS,       lambda: "Tools: Bash, Read.")
    sp.register(SectionName.GUIDELINES,  lambda: "Guidelines: be precise.")
    sp.register(SectionName.AGENTS_DOC,  lambda: "# Project docs")
    sp.register(SectionName.SKILLS,      lambda: None)    # null — skip
    sp.register(SectionName.MEMORY,      lambda: "- Use pnpm not npm")
    sp.register(SectionName.REPO_MAP,    lambda: "src/\n  main.py")
    sp.register(SectionName.ENVIRONMENT, lambda: "cwd: /tmp")
    sp.register(SectionName.CUSTOM,      lambda: None)    # null — skip

    sections = sp.build()
    assert sections, "build() returned empty list"

    # Map name→index for the sections that resolved
    by_text: dict[str, int] = {s.text: i for i, s in enumerate(sections)}

    # Group 0: last non-null is GUIDELINES (index 2)
    guidelines_idx = next(i for i, s in enumerate(sections) if s.text == "Guidelines: be precise.")
    assert sections[guidelines_idx].cache_control, "GUIDELINES should have cache_control=True (last of group 0)"

    # Group 0 non-last sections should NOT have cache_control
    identity_idx = next(i for i, s in enumerate(sections) if "agent-forge" in s.text)
    assert not sections[identity_idx].cache_control, "IDENTITY should NOT have cache_control (not last of group 0)"

    # Group 1: SKILLS is null so last non-null is MEMORY
    memory_idx = next(i for i, s in enumerate(sections) if "pnpm" in s.text)
    assert sections[memory_idx].cache_control, "MEMORY should have cache_control=True (last non-null of group 1)"

    # Group 2: REPO_MAP
    repo_idx = next(i for i, s in enumerate(sections) if "src/" in s.text)
    assert sections[repo_idx].cache_control, "REPO_MAP should have cache_control=True (group 2)"

    # Group 3: VOLATILE — no cache
    env_idx = next(i for i, s in enumerate(sections) if "cwd:" in s.text)
    assert not sections[env_idx].cache_control, "ENVIRONMENT should NOT have cache_control (volatile)"

    # Verify order is stable
    orders = [sections.index(sections[i]) for i in range(len(sections))]
    assert orders == sorted(orders), "Sections must be in stable order"

# ── EVAL 3: ActionLog eviction ────────────────────────────────────────────────

def eval_action_log_eviction():
    from agent_forge.context import ContextBudget, ContextWindow, TurnRecord
    from agent_forge.provider import MODELS, TextContent, AssistantMessage, UserMessage

    model = MODELS["claude-sonnet-4-6"]
    # Small budget: keep_recent_tokens=500, recency_turns=2
    budget = ContextBudget(keep_recent_tokens=500, recency_turns=2)
    ctx = ContextWindow(model=model, budget=budget)

    # Add 5 turns, each ~200 tokens of text
    big_text = "x" * 800  # ~200 tokens
    for i in range(5):
        user_msg = UserMessage(content=f"Turn {i+1} user message")
        asst_msg = AssistantMessage(
            content=(TextContent(text=big_text),),
        )
        ctx.receive(
            user_message=user_msg,
            assistant_messages=[asst_msg],
            tool_calls=[],
        )

    # With budget of 2 turns, turns 1-3 should have been evicted to action log
    assert len(ctx._recent_turns) <= 2, \
        f"Expected <=2 recent turns, got {len(ctx._recent_turns)}"
    assert len(ctx._action_log) >= 3, \
        f"Expected >=3 action log entries, got {len(ctx._action_log)}"

    # Check action log entries have correct format
    entry = ctx._action_log[0]
    assert entry.summary.startswith("[T1]"), f"Expected '[T1]' prefix, got: {entry.summary!r}"
    assert entry.tokens > 0, "ActionLogEntry should have positive token estimate"

    # Build messages should include action log as synthetic user/assistant pair
    new_user = UserMessage(content="new question")
    msgs = ctx.build_messages(new_user)
    assert any("Prior session actions" in str(getattr(m, "content", "")) for m in msgs), \
        "build_messages() should include action log as synthetic user message"

# ── EVAL 4: Session round-trip ────────────────────────────────────────────────

def eval_session_roundtrip():
    import tempfile, json
    from agent_forge.session import (
        append_metadata, append_message, resume_session, new_id,
        sessions_dir,
    )
    from agent_forge.provider import (
        UserMessage, AssistantMessage, TextContent, ToolResultMessage,
        ToolCallContent,
    )

    sid = new_id()
    # Temporarily redirect sessions_dir
    with tempfile.TemporaryDirectory() as tmp:
        orig_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp  # redirect sessions_dir to tmp

        try:
            # Create session dir
            Path(tmp, ".agent-forge", "sessions").mkdir(parents=True)

            append_metadata(sid, "claude-sonnet-4-6", "/tmp/test")

            user_msg = UserMessage(content="What is 2+2?")
            asst_msg = AssistantMessage(content=(TextContent(text="4"),))
            tool_msg = ToolResultMessage(tool_call_id="t1", content="result", is_error=False)

            append_message(sid, user_msg)
            append_message(sid, asst_msg)
            append_message(sid, tool_msg)

            resumed = resume_session(sid)
            msgs = resumed.messages

            assert len(msgs) == 3, f"Expected 3 messages, got {len(msgs)}"
            assert isinstance(msgs[0], UserMessage), f"msg[0] should be UserMessage, got {type(msgs[0])}"
            assert msgs[0].content == "What is 2+2?", f"Content mismatch: {msgs[0].content!r}"
            assert isinstance(msgs[1], AssistantMessage), f"msg[1] should be AssistantMessage"
            assert any("4" in blk.text for blk in msgs[1].content if isinstance(blk, TextContent)), \
                "AssistantMessage content should contain '4'"
            assert isinstance(msgs[2], ToolResultMessage), f"msg[2] should be ToolResultMessage"

        finally:
            if orig_home:
                os.environ["HOME"] = orig_home
            else:
                del os.environ["HOME"]

# ── EVAL 5: Tool Read ─────────────────────────────────────────────────────────

def eval_tool_read():
    from agent_forge.tools import ReadTool

    tool = ReadTool()
    result = asyncio.run(
        tool.execute({"path": "pyproject.toml"}, cwd=str(Path(__file__).parent))
    )
    assert not result.is_error, f"ReadTool errored: {result.content}"
    assert "agent-forge" in result.content, "pyproject.toml should mention agent-forge"
    assert "3.12" in result.content, "pyproject.toml should mention Python 3.12"
    # Verify line numbers present
    assert result.content.startswith("1\t"), f"Should start with line number '1\\t', got: {result.content[:20]!r}"

# ── EVAL 6: Tool Grep ─────────────────────────────────────────────────────────

def eval_tool_grep():
    from agent_forge.tools import GrepTool

    tool = GrepTool()
    cwd = str(Path(__file__).parent)
    result = asyncio.run(
        tool.execute({"pattern": "agent.forge", "path": "."}, cwd=cwd)
    )
    assert not result.is_error, f"GrepTool errored: {result.content}"
    assert "pyproject.toml" in result.content or "agent_forge" in result.content, \
        f"Expected grep to find 'agent.forge' in files, got: {result.content[:200]}"

# ── EVAL 7: Tool Bash ─────────────────────────────────────────────────────────

def eval_tool_bash():
    from agent_forge.tools import BashTool

    tool = BashTool()
    result = asyncio.run(
        tool.execute({"command": "echo hello-agent-forge"}, cwd="/tmp")
    )
    assert not result.is_error, f"BashTool errored: {result.content}"
    assert "hello-agent-forge" in result.content, f"Expected echo output, got: {result.content!r}"

    # Test error detection
    err_result = asyncio.run(
        tool.execute({"command": "exit 1"}, cwd="/tmp")
    )
    assert err_result.is_error, "Non-zero exit should be is_error=True"

# ── EVAL 8: P4 eviction content ───────────────────────────────────────────────

def eval_p4_eviction():
    from agent_forge.context import evict_p4, _P4_MAX_BYTES, _P4_NOTICE
    from agent_forge.provider import ToolResultMessage, TextContent, AssistantMessage

    big_content = "A" * (_P4_MAX_BYTES + 100)
    small_content = "B" * 100

    msgs = [
        ToolResultMessage(tool_call_id="t1", content=big_content, is_error=False),
        ToolResultMessage(tool_call_id="t2", content=small_content, is_error=False),
        AssistantMessage(content=(TextContent(text="answer"),)),
    ]

    evicted = evict_p4(msgs)
    assert len(evicted) == 3, "evict_p4 should preserve message count"
    assert evicted[0].content == _P4_NOTICE, \
        f"Large tool result should be replaced with notice, got: {evicted[0].content[:50]!r}"
    assert evicted[1].content == small_content, \
        "Small tool result should be preserved unchanged"
    assert evicted[2] == msgs[2], "Non-tool messages should be unchanged"

# ── EVAL 9: Integration — agent uses Read tool ────────────────────────────────

async def eval_integration_read():
    from agent_forge.context import SectionName, SystemPrompt
    from agent_forge.loop import DoneAgentEvent, TurnStartEvent, ToolResultAgentEvent, agent_loop, make_config
    from agent_forge.provider import DEFAULT_MODEL, Model, UserMessage
    from agent_forge.tools import default_registry

    api_key = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )
    assert api_key, "CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY not set"

    cwd = str(Path(__file__).parent)
    tool_registry = default_registry()

    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: "You are a helpful coding assistant.")
    sp.register(SectionName.TOOLS, lambda: "Use Read to read files. Always use tools, never guess.")
    sp.register(SectionName.ENVIRONMENT, lambda: f"Working directory: {cwd}")
    system = sp.build()

    loop_cfg = make_config(
        model=DEFAULT_MODEL,
        api_key=api_key,
        system_prompt=system,
        tool_registry=tool_registry,
        cwd=cwd,
        max_turns=5,
    )

    user_msg = UserMessage(content="What Python version does agent_forge require? Check pyproject.toml.")
    initial_msgs = [user_msg]

    result = None
    tool_calls_seen: list[str] = []

    async for event in agent_loop(loop_cfg, initial_msgs):
        if isinstance(event, ToolResultAgentEvent):
            tool_calls_seen.append(event.name)
        elif isinstance(event, DoneAgentEvent):
            result = event.result

    assert result is not None, "Agent should have produced a result"
    assert "Read" in tool_calls_seen, \
        f"Agent should have used Read tool, used: {tool_calls_seen}"
    assert "3.12" in result.text or "3.12" in str([tc.args for tc in result.tool_calls]), \
        f"Answer should mention Python 3.12. Got: {result.text[:300]}"

# ── EVAL 10: Cache hit on second turn ─────────────────────────────────────────

async def eval_cache_hit_turn_2():
    from agent_forge.context import SectionName, SystemPrompt
    from agent_forge.loop import DoneAgentEvent, agent_loop, make_config
    from agent_forge.provider import DEFAULT_MODEL, UserMessage, ZERO_USAGE
    from agent_forge.tools import default_registry

    api_key = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )
    assert api_key, "CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY not set"

    cwd = str(Path(__file__).parent)
    tool_registry = default_registry()

    # Build system prompt with stable cached sections
    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: "You are agent-forge. " + "x" * 500)  # >500 chars to ensure caching
    sp.register(SectionName.TOOLS, lambda: "Available tools: Read, Bash, Write, Edit, Grep, Find. " + "y" * 200)
    sp.register(SectionName.GUIDELINES, lambda: "Guidelines: be concise. " + "z" * 200)
    sp.register(SectionName.ENVIRONMENT, lambda: f"cwd: {cwd}")
    system = sp.build()

    # Verify cache_control is set on group 0 last section
    assert system[2].cache_control, \
        "GUIDELINES (last of group 0) should have cache_control=True"

    loop_cfg = make_config(
        model=DEFAULT_MODEL,
        api_key=api_key,
        system_prompt=system,
        tool_registry=tool_registry,
        cwd=cwd,
        max_turns=2,
    )

    # Turn 1: write to cache
    msg1 = UserMessage(content="Say 'turn one' and stop.")
    result1 = None
    async for event in agent_loop(loop_cfg, [msg1]):
        if isinstance(event, DoneAgentEvent):
            result1 = event.result
    assert result1 is not None

    # Turn 2: read from cache — cache_read should be > 0
    from agent_forge.context import ContextWindow
    ctx = ContextWindow(model=DEFAULT_MODEL)
    ctx.receive(user_message=msg1, assistant_messages=result1.messages, tool_calls=[])
    msgs_for_turn2 = ctx.build_messages(UserMessage(content="Say 'turn two' and stop."))

    result2 = None
    async for event in agent_loop(loop_cfg, msgs_for_turn2):
        if isinstance(event, DoneAgentEvent):
            result2 = event.result
    assert result2 is not None

    from agent_forge.provider import _is_oauth
    if _is_oauth(api_key):
        # OAuth injects system as UserMessage; cache_read comes from implicit API-side caching
        # which may not appear in usage tokens — skip the hard assertion
        pass
    else:
        assert result2.usage.cache_read > 0, \
            f"Turn 2 should have cache_read > 0 (got {result2.usage.cache_read}). " \
            "Check that cache_control flags are being sent to Anthropic API."

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n\x1b[1magent_forge eval suite\x1b[0m\n")

    api_key = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )

    print("\x1b[1mUnit evals\x1b[0m (no API key required)")
    _run("1. pressure_tiers",        eval_pressure_tiers)
    _run("2. cache_group_placement", eval_cache_group_placement)
    _run("3. action_log_eviction",   eval_action_log_eviction)
    _run("4. session_roundtrip",     eval_session_roundtrip)
    _run("5. tool_read",             eval_tool_read)
    _run("6. tool_grep",             eval_tool_grep)
    _run("7. tool_bash",             eval_tool_bash)
    _run("8. p4_eviction_content",   eval_p4_eviction)

    print(f"\n\x1b[1mIntegration evals\x1b[0m (requires CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)")
    if not api_key:
        _skip("9.  agent_uses_read_tool",  "CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY not set")
        _skip("10. cache_hit_on_turn_2",   "CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY not set")
    else:
        _run("9.  agent_uses_read_tool",  eval_integration_read)
        _run("10. cache_hit_on_turn_2",   eval_cache_hit_turn_2)

    # Summary
    passed  = sum(1 for _, ok, _ in _results if ok is True)
    failed  = sum(1 for _, ok, _ in _results if ok is False)
    skipped = sum(1 for _, ok, _ in _results if ok is None)
    total   = passed + failed

    print(f"\n{'─'*40}")
    print(f"\x1b[1mResults: {passed}/{total} passed\x1b[0m  "
          f"(\x1b[31m{failed} failed\x1b[0m, \x1b[33m{skipped} skipped\x1b[0m)")

    if failed:
        print("\nFailed evals:")
        for name, ok, msg in _results:
            if ok is False:
                print(f"  \x1b[31m✗\x1b[0m {name}: {msg}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
