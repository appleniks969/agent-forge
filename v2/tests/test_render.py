"""Renderer tests: channel separation (the 391391 fix) + markdown rendering.

The renderer is driven on a non-terminal (StringIO) so Rich renders once per
block with no cursor control — deterministic, captures the final text.
"""

from __future__ import annotations

import io

from forge.front.render import Renderer
from forge.kernel.events import (
    Envelope,
    TextDelta,
    ThinkingDelta,
    ToolDeclared,
    TurnFinished,
)
from forge.kernel.types import TextBlock, ToolCall, Usage


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
    assert "10 in / 5 out" in text


def test_assistant_block_is_not_double_rendered():
    # AssistantBlock carries the same text already streamed as a delta; it must
    # not re-render (the renderer ignores it).
    from forge.kernel.events import AssistantBlock

    text = _drive([TextDelta("answer"), AssistantBlock(block=TextBlock(text="answer"))])
    assert text.count("answer") == 1
