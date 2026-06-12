"""Renderer: a bus subscriber that draws envelopes as plain ANSI text.

Layer: front — imports kernel only. The Renderer is an OBJECT with its own
line-state buffer (no module globals): text deltas stream raw, thinking
streams dim, tool calls get one-liners, and TurnFinished prints a footer
with tokens plus cost when pricing is known (the kernel always logs cost
as None — pricing is provider knowledge injected by wiring). AssistantBlock
events are ignored: their text already streamed as deltas.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any, TextIO

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
from forge.kernel.types import Pricing, ToolResult

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"

_ARG_VALUE_MAX = 40
_BRIEF_MAX = 100


def args_brief(args: Mapping[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", " ")
        if len(text) > _ARG_VALUE_MAX:
            text = text[: _ARG_VALUE_MAX - 3] + "..."
        parts.append(f"{key}={text}")
    brief = ", ".join(parts)
    if len(brief) > _BRIEF_MAX:
        brief = brief[: _BRIEF_MAX - 3] + "..."
    return brief


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


class Renderer:
    def __init__(
        self,
        out: TextIO | None = None,
        *,
        pricing: Pricing | None = None,
        color: bool | None = None,
    ) -> None:
        self._out = out if out is not None else sys.stdout
        self._pricing = pricing
        if color is None:
            isatty = getattr(self._out, "isatty", None)
            color = bool(isatty()) if callable(isatty) else False
        self._color = color
        self._mid_line = False

    def handle(self, env: Envelope) -> None:
        match env.body:
            case TextDelta(text):
                self._stream(text)
            case ThinkingDelta(text):
                self._stream(text, dim=True)
            case ToolDeclared(call):
                self._line(f"* {call.name}({args_brief(call.args)})", dim=True)
            case ToolFinished(result):
                self._tool_finished(result)
            case PermissionAsked(question):
                self._line(f"? {question.tool}: {question.question}")
            case PermissionDecided(allowed=allowed, source=source, reason=reason):
                verdict = "allowed" if allowed else "denied"
                note = f" ({reason})" if reason else ""
                self._line(f"  {verdict} by {source}{note}", dim=True)
            case RetryScheduled(attempt=attempt, delay_s=delay, reason=reason):
                self._line(f"retry {attempt} in {delay:.1f}s: {reason}", dim=True)
            case TurnFinished():
                self._footer(env.body)
            case _:
                pass  # AssistantBlock duplicates deltas; the rest carry no UI

    def _tool_finished(self, result: ToolResult) -> None:
        if result.is_error:
            self._line(f"  ! {_first_line(result.content)}", dim=True)

    def _footer(self, ev: TurnFinished) -> None:
        usage = ev.usage
        cost = ev.cost
        if cost is None and self._pricing is not None:
            cost = self._pricing.cost(usage)
        parts = [ev.outcome, f"{usage.input_tokens} in / {usage.output_tokens} out"]
        if usage.cache_read_tokens or usage.cache_write_tokens:
            parts.append(
                f"cache {usage.cache_read_tokens}r/{usage.cache_write_tokens}w"
            )
        if cost is not None:
            parts.append(f"${cost:.4f}")
        self._line("-- " + " | ".join(parts), dim=True)

    # -- line-state plumbing ----------------------------------------------------

    def _paint(self, text: str, dim: bool) -> str:
        return f"{_DIM}{text}{_RESET}" if dim and self._color else text

    def _stream(self, text: str, *, dim: bool = False) -> None:
        if not text:
            return
        self._out.write(self._paint(text, dim))
        self._out.flush()
        self._mid_line = not text.endswith("\n")

    def _line(self, text: str, *, dim: bool = False) -> None:
        if self._mid_line:
            self._out.write("\n")
        self._out.write(self._paint(text, dim) + "\n")
        self._out.flush()
        self._mid_line = False
