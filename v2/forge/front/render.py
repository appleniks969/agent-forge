"""Renderer: a bus subscriber that draws envelopes with Rich.

Layer: front — imports kernel only. The Renderer is an OBJECT with its own
streaming state (no module globals). Model answers stream as live Markdown
(code blocks syntax-highlighted, tables aligned); thinking streams dim in a
SEPARATE region above the answer, so reasoning and answer never run together
(the old plain renderer concatenated 'thinking' + 'answer' on one line).
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

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
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
        self._think = ""  # accumulated thinking text for the current run
        self._text = ""  # accumulated answer markdown for the current run
        self._live: Live | None = None

    # -- event dispatch ---------------------------------------------------------

    def handle(self, env: Envelope) -> None:
        match env.body:
            case TextDelta(text):
                self._text += text
                self._refresh()
            case ThinkingDelta(text):
                self._think += text
                self._refresh()
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

    # -- streaming region (thinking + answer) -----------------------------------

    def _renderable(self) -> RenderableType:
        parts: list[RenderableType] = []
        if self._think:
            # While thinking streams (no answer yet), show it dim and live.
            # Once the answer starts, collapse it to a one-line marker so the
            # rendered answer is what stays in scrollback.
            if self._text:
                parts.append(Text("✓ thought", style="dim"))
            else:
                parts.append(Text(self._think, style="dim"))
        if self._text:
            parts.append(Markdown(self._text))
        return Group(*parts)

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
        """End the current streaming run: finalize the Live region (terminal)
        or print the accumulated content once (piped), then reset buffers."""
        if not self._think and not self._text:
            return
        if self._live is not None:
            self._live.update(self._renderable())
            self._live.stop()
            self._live = None
        else:
            # Non-terminal: render the answer once; thinking is omitted from
            # captured output to keep --json/test streams to the answer + footer.
            if self._text:
                self._console.print(Markdown(self._text))
        self._think = ""
        self._text = ""

    # -- discrete lines ---------------------------------------------------------

    def _tool_finished(self, result: ToolResult) -> None:
        if result.is_error:
            self._flush()
            self._console.print(f"  ! {_first_line(result.content)}", style="dim red")

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
        self._console.print("-- " + " | ".join(parts), style="dim")
