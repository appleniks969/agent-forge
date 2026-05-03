"""Tests for renderer.render_event() — exhaustiveness over the AgentEvent
union, text-buffer flushing rules, and footer formatting.

These are smoke/snapshot-style tests: we don't pin the exact bytes of the
ANSI output (it's a UI surface and brittle), we just assert the right
markers appear and that every AgentEvent variant is handled without raising.
"""
from __future__ import annotations

import pytest

from agent_forge import renderer as r
from agent_forge.loop import (
    AbortedAgentEvent, AgentResult, CompactionAgentEvent, DoneAgentEvent,
    ErrorAgentEvent, TextDeltaAgentEvent, TextEndAgentEvent, TextStartAgentEvent,
    ThinkingDeltaAgentEvent, ThinkingEndAgentEvent, ThinkingStartAgentEvent,
    ToolBlockedAgentEvent, ToolDeclaredAgentEvent, ToolExecutingAgentEvent,
    ToolResultAgentEvent, TurnEndEvent, TurnStartEvent,
    ToolCallRecord,
)
from agent_forge.messages import TokenUsage, ToolResult, ZERO_USAGE


# ── Helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_text_buffer():
    """The renderer keeps a module-level _text_buffer. Tests must not leak."""
    r._text_buffer.clear()
    yield
    r._text_buffer.clear()


def _render(event, *, verbose: bool = False) -> None:
    r.render_event(event, verbose=verbose)


# ── ANSI helpers ─────────────────────────────────────────────────────────────


class TestAnsiHelpers:
    def test_dim_wraps_with_reset(self):
        assert r.dim("x") == "\x1b[2mx\x1b[0m"

    def test_bold_green_red_yellow_cyan(self):
        for fn in (r.bold, r.green, r.red, r.yellow, r.cyan):
            wrapped = fn("text")
            assert wrapped.startswith("\x1b[")
            assert wrapped.endswith("\x1b[0m")
            assert "text" in wrapped


# ── _fmt_bytes / _key_arg internals ──────────────────────────────────────────


class TestKeyArg:
    def test_bash_key_is_command(self):
        assert "ls -la" in r._key_arg("Bash", {"command": "ls -la"})

    def test_long_command_truncated(self):
        out = r._key_arg("Bash", {"command": "x" * 200})
        assert "…" in out
        assert len(out) < 200

    def test_read_uses_path(self):
        assert "src/x.py" in r._key_arg("Read", {"path": "src/x.py"})

    def test_grep_quotes_pattern(self):
        out = r._key_arg("Grep", {"pattern": "TODO"})
        assert "TODO" in out

    def test_unknown_tool_returns_empty(self):
        assert r._key_arg("Mystery", {"foo": "bar"}) == ""


class TestFmtBytes:
    def test_bytes(self):
        assert r._fmt_bytes(0) == "0 B"
        assert r._fmt_bytes(512) == "512 B"

    def test_kilobytes(self):
        assert "KB" in r._fmt_bytes(2048)

    def test_megabytes(self):
        assert "MB" in r._fmt_bytes(2 * 1024 * 1024)


# ── Event rendering: exhaustiveness over AgentEvent union ────────────────────


class TestEventRendering:
    """One test per branch — locks the AgentEvent union exhaustiveness in
    render_event(). If a branch is added to AgentEvent without updating
    render_event, the new test for it (added alongside) will fail; if a
    branch is removed, dead code can be deleted with confidence."""

    def test_turn_start(self, capfd):
        _render(TurnStartEvent(turn=3))
        out = capfd.readouterr().out
        assert "Turn 3" in out

    def test_turn_end_with_duration(self, capfd):
        _render(TurnEndEvent(turn=3, duration_ms=1234.0))
        out = capfd.readouterr().out
        assert "1.2s" in out

    def test_turn_end_zero_duration_silent(self, capfd):
        _render(TurnEndEvent(turn=3, duration_ms=0.0))
        out = capfd.readouterr().out
        assert "s" not in out  # nothing printed

    def test_thinking_start_emits_marker(self, capfd):
        _render(ThinkingStartAgentEvent(index=0))
        out = capfd.readouterr().out
        assert "💭" in out
        assert "thinking" in out

    def test_thinking_delta_writes_in_place(self, capfd):
        _render(ThinkingDeltaAgentEvent(delta="reasoning"))
        out = capfd.readouterr().out
        assert "reasoning" in out

    def test_thinking_end_resets_dim(self, capfd):
        _render(ThinkingEndAgentEvent(index=0))
        out = capfd.readouterr().out
        assert "\x1b[0m" in out  # reset SGR

    def test_text_start_silent(self, capfd):
        _render(TextStartAgentEvent(index=0))
        assert capfd.readouterr().out == ""

    def test_text_delta_buffered_not_printed(self, capfd):
        _render(TextDeltaAgentEvent(delta="hello "))
        _render(TextDeltaAgentEvent(delta="world"))
        # Nothing flushed yet
        assert capfd.readouterr().out == ""
        # But the buffer holds it
        assert "".join(r._text_buffer) == "hello world"

    def test_text_end_flushes_buffer_as_markdown(self, capfd):
        _render(TextDeltaAgentEvent(delta="# heading\n\nbody"))
        _render(TextEndAgentEvent(index=0))
        out = capfd.readouterr().out
        # Bullet marker '●' precedes the markdown render
        assert "●" in out
        assert "heading" in out  # rich strips '#' for terminal display
        # Buffer is cleared
        assert r._text_buffer == []

    def test_text_end_with_empty_buffer_silent(self, capfd):
        _render(TextEndAgentEvent(index=0))
        assert "●" not in capfd.readouterr().out

    def test_text_end_with_whitespace_only_silent(self, capfd):
        _render(TextDeltaAgentEvent(delta="   \n  "))
        _render(TextEndAgentEvent(index=0))
        # Whitespace-only buffer should not print bullet marker
        assert "●" not in capfd.readouterr().out

    def test_tool_declared_prints_name_and_arg(self, capfd):
        _render(ToolDeclaredAgentEvent(id="t1", name="Read", args={"path": "x.txt"}))
        out = capfd.readouterr().out
        assert "Read" in out
        assert "x.txt" in out
        assert "⚙" in out

    def test_tool_declared_flushes_pending_text(self, capfd):
        """If a TextDelta was buffered without a TextEnd, ToolDeclared must
        flush it before printing the tool line."""
        _render(TextDeltaAgentEvent(delta="thinking out loud"))
        _render(ToolDeclaredAgentEvent(id="t1", name="Read", args={"path": "x"}))
        out = capfd.readouterr().out
        assert "thinking out loud" in out  # text was flushed
        assert "Read" in out
        assert r._text_buffer == []  # buffer emptied

    def test_tool_executing_silent(self, capfd):
        _render(ToolExecutingAgentEvent(id="t1", name="Read", args={"path": "x"}))
        assert capfd.readouterr().out == ""

    def test_tool_result_success_shows_size(self, capfd):
        _render(ToolResultAgentEvent(
            id="t1", name="Read",
            result=ToolResult(content="hello world", is_error=False),
        ))
        out = capfd.readouterr().out
        assert "✓" in out
        assert "B" in out  # byte/KB/MB marker

    def test_tool_result_error_shows_snippet(self, capfd):
        _render(ToolResultAgentEvent(
            id="t1", name="Read",
            result=ToolResult(content="ENOENT: file not found", is_error=True),
        ))
        out = capfd.readouterr().out
        assert "✗" in out
        assert "ENOENT" in out

    def test_tool_blocked_shows_reason(self, capfd):
        _render(ToolBlockedAgentEvent(
            id="t1", name="Bash", reason="destructive command",
        ))
        out = capfd.readouterr().out
        assert "⊘" in out
        assert "Bash" in out
        assert "destructive command" in out

    def test_error_retryable_uses_retry_marker(self, capfd):
        _render(ErrorAgentEvent(error="rate limited", retryable=True))
        out = capfd.readouterr().out
        assert "⟳" in out
        assert "rate limited" in out

    def test_error_fatal_uses_x_marker(self, capfd):
        _render(ErrorAgentEvent(error="auth failed", retryable=False))
        out = capfd.readouterr().out
        assert "✗" in out
        assert "auth failed" in out

    def test_error_clears_text_buffer(self, capfd):
        """If an error fires while text was streaming, the half-streamed
        text must not survive into the next turn."""
        _render(TextDeltaAgentEvent(delta="partial sentence"))
        _render(ErrorAgentEvent(error="boom", retryable=True))
        capfd.readouterr()  # discard
        assert r._text_buffer == []
        # Subsequent TextEnd must NOT flush the now-discarded partial text
        _render(TextEndAgentEvent(index=0))
        out = capfd.readouterr().out
        assert "partial sentence" not in out

    def test_aborted_clears_text_buffer(self, capfd):
        _render(TextDeltaAgentEvent(delta="partial"))
        _render(AbortedAgentEvent())
        out = capfd.readouterr().out
        assert "Interrupted" in out
        assert "⚠" in out
        assert r._text_buffer == []

    def test_compaction_silent_unless_verbose(self, capfd):
        ev = CompactionAgentEvent(tokens_before=100, tokens_after=50)
        _render(ev, verbose=False)
        assert capfd.readouterr().out == ""

    def test_compaction_verbose_shows_token_counts(self, capfd):
        ev = CompactionAgentEvent(tokens_before=100, tokens_after=50)
        _render(ev, verbose=True)
        out = capfd.readouterr().out
        assert "100" in out
        assert "50" in out
        assert "compaction" in out

    def test_done_silent(self, capfd):
        result = AgentResult(
            text="done", tool_calls=[], messages=[], usage=ZERO_USAGE,
            turns=1, aborted=False,
        )
        _render(DoneAgentEvent(result=result))
        # Footer is the caller's job; renderer is silent here
        assert capfd.readouterr().out == ""


# ── Footer formatting ────────────────────────────────────────────────────────


class TestPrintFooter:
    def test_basic_fields_render(self, capfd):
        usage = TokenUsage(input=1234, output=567, cache_read=1000, cache_write=500, cost=0.0)
        r.print_footer(
            model_id="claude-sonnet-4-6", session_cost=0.0123, usage=usage,
            turns=4, ctx_pct=12.0,
        )
        out = capfd.readouterr().out
        assert "4 turn" in out
        assert "1,234" in out  # input formatted with comma
        assert "567" in out
        assert "$0.0123" in out
        assert "1,000" in out  # cache_read
        assert "500" in out    # cache_write
        assert "12%" in out

    def test_session_cache_appended_when_supplied(self, capfd):
        usage = TokenUsage(input=10, output=5, cache_read=0, cache_write=0)
        session_usage = TokenUsage(input=100, output=50, cache_read=8000, cache_write=4000)
        r.print_footer(
            model_id="x", session_cost=0.0, usage=usage,
            turns=1, ctx_pct=0.0, session_usage=session_usage,
        )
        out = capfd.readouterr().out
        assert "session cache" in out
        assert "8,000" in out
        assert "4,000" in out

    def test_session_cache_omitted_when_zero(self, capfd):
        usage = TokenUsage(input=1, output=1)
        session_usage = TokenUsage(input=2, output=2)  # no cache activity
        r.print_footer(
            model_id="x", session_cost=0.0, usage=usage,
            turns=1, ctx_pct=0.0, session_usage=session_usage,
        )
        assert "session cache" not in capfd.readouterr().out


# ── Rich console singleton ───────────────────────────────────────────────────


def test_get_console_returns_singleton():
    a = r.get_console()
    b = r.get_console()
    assert a is b


# ── render_markdown silent on empty input ────────────────────────────────────


def test_render_markdown_empty_silent(capfd):
    r.render_markdown("")
    r.render_markdown("   \n  ")
    assert capfd.readouterr().out == ""
