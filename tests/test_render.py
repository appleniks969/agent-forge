"""Renderer tests: channel separation (the 391391 fix) + markdown rendering.

The renderer is driven on a non-terminal (StringIO) so Rich renders once per
block with no cursor control — deterministic, captures the final text.
"""

from __future__ import annotations

import io

from forge.front.render import Renderer
from forge.kernel.events import (
    Envelope,
    PermissionAsked,
    PermissionDecided,
    RetryScheduled,
    TextDelta,
    ThinkingDelta,
    ToolDeclared,
    ToolFinished,
    TurnFinished,
)
from forge.kernel.types import PermissionQuestion, Pricing, TextBlock, ToolCall, ToolResult, Usage


def _env(body):
    return Envelope(seq=0, sid="s", parent=None, ts=0.0, v=1, durable=False, body=body)


def _drive(bodies):
    # Every real turn ends with TurnFinished, which flushes the streaming
    # region; append one unless the sequence already finishes itself.
    out = io.StringIO()
    r = Renderer(out=out, color=False)
    seq = list(bodies)
    if not seq or not isinstance(seq[-1], TurnFinished):
        seq.append(
            TurnFinished(outcome="ok", usage=Usage(input_tokens=0, output_tokens=0), cost=None)
        )
    for b in seq:
        r.handle(_env(b))
    return out.getvalue()


def test_thinking_and_answer_do_not_concatenate():
    # The 391391 regression: thinking '391' then answer '391' must NOT render
    # as '391391' — they belong to separate regions.
    text = _drive([ThinkingDelta("391"), TextDelta("391")])
    assert "391391" not in text
    assert "391" in text  # the answer still renders


def test_thinking_renders_as_a_visible_block():
    # Thinking content is shown in its own block (not collapsed to a marker),
    # separate from the answer.
    text = _drive([ThinkingDelta("weighing options"), TextDelta("the answer")])
    assert "thinking" in text  # the block label
    assert "weighing options" in text  # the reasoning is visible
    assert "the answer" in text


def test_answer_renders_as_markdown_heading():
    text = _drive([TextDelta("# Title\n\nbody")])
    assert "Title" in text
    assert "#" not in text  # the heading marker is consumed by markdown rendering


def test_code_block_content_survives_rendering():
    text = _drive([TextDelta("```python\nx = 1\n```")])
    assert "x = 1" in text


def test_thinking_flushed_before_a_tool_line():
    text = _drive(
        [
            ThinkingDelta("let me look"),
            ToolDeclared(ToolCall(id="t1", name="Read", args={"path": "a.py"})),
            TextDelta("done"),
        ]
    )
    assert "Read" in text and "done" in text


def test_footer_reports_outcome_and_tokens():
    text = _drive(
        [
            TextDelta("hi"),
            TurnFinished(
                outcome="ok",
                usage=Usage(input_tokens=10, output_tokens=5),
                cost=None,
            ),
        ]
    )
    assert "ok" in text
    assert "↑10" in text and "↓5" in text  # token badge


def test_assistant_block_is_not_double_rendered():
    # AssistantTurn carries the same text already streamed as a delta; it must
    # not re-render (the renderer ignores it).
    from forge.kernel.events import AssistantTurn

    text = _drive(
        [TextDelta("answer"), AssistantTurn(blocks=(TextBlock(text="answer"),), usage=Usage())]
    )
    assert text.count("answer") == 1


def test_permission_and_retry_lines_render():
    text = _drive(
        [
            PermissionAsked(PermissionQuestion("c1", "Bash", "run ls?")),
            PermissionDecided(call_id="c1", allowed=True, source="user", reason=""),
            RetryScheduled(attempt=1, delay_s=0.5, reason="overloaded"),
        ]
    )
    assert "Bash" in text and "run ls?" in text
    assert "allowed" in text and "user" in text
    assert "retry 1" in text and "overloaded" in text


def test_tool_error_line_renders():
    text = _drive(
        [
            ToolDeclared(ToolCall(id="t1", name="Read", args={"path": "a.py"})),
            ToolFinished(ToolResult(call_id="t1", content="no such file\nmore", is_error=True)),
        ]
    )
    assert "Read" in text
    assert "no such file" in text
    assert "more" not in text  # first line only


def test_footer_reports_cache_and_priced_cost():
    out = io.StringIO()
    r = Renderer(out=out, color=False, pricing=Pricing(2.0, 10.0, 0.2, 2.5))
    r.handle(
        _env(
            TurnFinished(
                outcome="ok",
                usage=Usage(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_tokens=100,
                    cache_write_tokens=20,
                ),
                cost=None,
            )
        )
    )
    text = out.getvalue()
    assert "ok" in text
    assert "cache 100/20" in text
    assert "$" in text


def test_note_dropped_is_visible():
    out = io.StringIO()
    r = Renderer(out=out, color=False)
    r.note_dropped(3)
    assert "dropped 3" in out.getvalue()
