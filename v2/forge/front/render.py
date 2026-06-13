"""Renderer: a bus subscriber that draws envelopes with Rich.

Layer: front — imports kernel only. The Renderer is an OBJECT with its own
streaming state (no module globals). A turn renders as two distinct blocks:
the model's reasoning in a dim bordered "thinking" panel above, and the answer
rendered as Markdown below (code blocks syntax-highlighted, tables aligned).
They never run together (the old plain renderer concatenated 'thinking' +
'answer' on one line — the 391391 bug).
Tool calls get one-liners, and TurnFinished prints a footer with tokens plus
cost when pricing is known (the kernel always logs cost as None — pricing is
provider knowledge injected by wiring). AssistantBlock events are ignored:
their text already streamed as deltas.

Rich's Live region is used only on a real terminal; piped/captured output
accumulates and renders once per block (no cursor control, no duplication),
which keeps --json eval output and tests clean.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any, TextIO

from rich.console import Console, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

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
        out = out if out is not None else sys.stdout
        # force_terminal=None lets Rich auto-detect; an explicit color= overrides.
        self._console = Console(file=out, force_terminal=color, highlight=False)
        self._pricing = pricing
        self._buf = ""  # text accumulated for the current block
        self._channel: str | None = None  # "think" | "text" — the current block
        self._live: Live | None = None

    # -- event dispatch ---------------------------------------------------------

    def handle(self, env: Envelope) -> None:
        match env.body:
            case TextDelta(text):
                self._stream(text, "text")
            case ThinkingDelta(text):
                self._stream(text, "think")
            case ToolDeclared(call):
                self._flush()
                self._console.print(
                    f"→ {call.name}({args_brief(call.args)})", style="cyan"
                )
            case ToolFinished(result):
                self._tool_finished(result)
            case PermissionAsked(question):
                self._flush()
                self._console.print(
                    f"? {question.tool}: {question.question}", style="yellow"
                )
            case PermissionDecided(allowed=allowed, source=source, reason=reason):
                self._flush()
                verdict = "allowed" if allowed else "denied"
                note = f" ({reason})" if reason else ""
                self._console.print(f"  {verdict} by {source}{note}", style="dim")
            case RetryScheduled(attempt=attempt, delay_s=delay, reason=reason):
                self._flush()
                self._console.print(
                    f"retry {attempt} in {delay:.1f}s: {reason}", style="dim yellow"
                )
            case TurnFinished():
                self._flush()
                self._footer(env.body)
            case _:
                pass  # AssistantBlock duplicates deltas; the rest carry no UI

    # -- streaming blocks (one block per channel run) ---------------------------

    def _stream(self, text: str, channel: str) -> None:
        # A channel switch (think→text or text→think) finalizes the current
        # block before the next one starts, so the thinking panel is sealed in
        # scrollback before the answer streams below it.
        if self._channel is not None and channel != self._channel:
            self._flush()
        self._channel = channel
        self._buf += text
        self._refresh()

    def _renderable(self) -> RenderableType:
        if self._channel == "think":
            return Panel(
                Text(self._buf, style="dim"),
                title="thinking",
                title_align="left",
                border_style="dim",
                padding=(0, 1),
            )
        return Markdown(self._buf)

    def _refresh(self) -> None:
        if not self._console.is_terminal:
            return  # piped/captured: defer to a single render at _flush
        if self._live is None:
            self._live = Live(
                self._renderable(),
                console=self._console,
                refresh_per_second=8,
                vertical_overflow="visible",
            )
            self._live.start()
        else:
            self._live.update(self._renderable())

    def _flush(self) -> None:
        """Seal the current block: finalize the Live region (terminal) or print
        the accumulated block once (piped), then reset for the next block."""
        if not self._buf:
            self._channel = None
            return
        if self._live is not None:
            self._live.update(self._renderable())
            self._live.stop()
            self._live = None
        else:
            self._console.print(self._renderable())  # non-terminal: render once
        self._buf = ""
        self._channel = None

    # -- discrete lines ---------------------------------------------------------

    def _tool_finished(self, result: ToolResult) -> None:
        if result.is_error:
            self._flush()
            self._console.print(f"  ! {_first_line(result.content)}", style="dim red")

    def print_banner(self, model: str, *, commands: str = "") -> None:
        """A solid header panel shown once at REPL start."""
        body = Text()
        body.append("model  ", style="dim")
        body.append(model, style="bold cyan")
        if commands:
            body.append("\n")
            body.append(commands, style="dim")
        self._console.print(
            Panel(body, title="forge", title_align="left", border_style="cyan", padding=(0, 1))
        )

    def _footer(self, ev: TurnFinished) -> None:
        usage = ev.usage
        cost = ev.cost
        if cost is None and self._pricing is not None:
            cost = self._pricing.cost(usage)
        ok = ev.outcome == "ok"
        badges: list[tuple[str, str]] = [
            (f" {ev.outcome} ", "black on green" if ok else "white on red"),
            (f" ↑{usage.input_tokens:,} ↓{usage.output_tokens:,} ", "black on bright_blue"),
        ]
        if usage.cache_read_tokens or usage.cache_write_tokens:
            badges.append(
                (
                    f" cache {usage.cache_read_tokens:,}/{usage.cache_write_tokens:,} ",
                    "black on grey62",
                )
            )
        if cost is not None:
            badges.append((f" ${cost:.4f} ", "black on magenta"))
        line = Text()
        for i, (label, style) in enumerate(badges):
            if i:
                line.append(" ")
            line.append(label, style=style)
        self._console.print(line)
