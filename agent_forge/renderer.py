"""
renderer.py — ANSI helpers, event renderer, markdown printer, footer.

Depends on loop (AgentEvent types) and provider (TokenUsage). Presentation-only
leaf of the composition layer — zero business logic, zero file I/O. Both chat.py
and autonomous.py import render_event(); neither needs to know the ANSI details.

Owns: dim/bold/green/red/yellow/cyan helpers, render_event() (handles all 16
      AgentEvent branches), render_markdown(), print_footer(), get_console()
      (Rich singleton).

Stream rendering strategy — stateless, driven by explicit block-lifecycle events:

  • ThinkingStartAgentEvent → print dim "💭 thinking" header, enter dim ANSI mode.
  • ThinkingDeltaAgentEvent → print characters in-place (scrollback-safe).
  • ThinkingEndAgentEvent   → close dim SGR + newline.

  • TextStartAgentEvent     → begin buffering (nothing printed yet).
  • TextDeltaAgentEvent     → append to _text_buffer.
  • TextEndAgentEvent       → flush _text_buffer as a single Rich Markdown block.

  • ToolDeclaredAgentEvent  → print complete "⚙ Name: key-arg …" line (one shot,
                              all args available because the event fires at
                              content_block_stop, after the model has committed).
  • ToolExecutingAgentEvent → (intentionally silent; distinct visual gap between
                              declaration and result gives natural timing cue).
  • ToolResultAgentEvent    → ✓ / ✗ with byte size or error snippet.

No module-level stream-kind guessing. _text_buffer is the only mutable state,
cleared at every TextEndAgentEvent.
"""
from __future__ import annotations

from .loop import (
    AbortedAgentEvent, DoneAgentEvent, ErrorAgentEvent,
    TextDeltaAgentEvent, TextEndAgentEvent, TextStartAgentEvent,
    ThinkingDeltaAgentEvent, ThinkingEndAgentEvent, ThinkingStartAgentEvent,
    ToolBlockedAgentEvent, ToolDeclaredAgentEvent, ToolExecutingAgentEvent,
    ToolResultAgentEvent, TurnEndEvent, TurnStartEvent,
    CompactionAgentEvent,
)
from .provider import TokenUsage

# ── ANSI helpers ──────────────────────────────────────────────────────────────

_R      = "\x1b[0m"
_BOLD   = "\x1b[1m"
_DIM    = "\x1b[2m"
_CYAN   = "\x1b[36m"
_GREEN  = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED    = "\x1b[31m"

def dim(s: str)    -> str: return f"{_DIM}{s}{_R}"
def bold(s: str)   -> str: return f"{_BOLD}{s}{_R}"
def cyan(s: str)   -> str: return f"{_CYAN}{s}{_R}"
def green(s: str)  -> str: return f"{_GREEN}{s}{_R}"
def yellow(s: str) -> str: return f"{_YELLOW}{s}{_R}"
def red(s: str)    -> str: return f"{_RED}{s}{_R}"

# ── Rich console singleton ────────────────────────────────────────────────────

_rich_console = None

def get_console():
    global _rich_console
    if _rich_console is None:
        from rich.console import Console
        _rich_console = Console()
    return _rich_console

# ── Text buffer (the only mutable renderer state) ─────────────────────────────
#
# Filled by TextDeltaAgentEvent, flushed as Markdown by TextEndAgentEvent.
# Cleared on every flush, and also on ErrorAgentEvent / AbortedAgentEvent to
# guard against stale partial content surviving a retry or abort.
#
# NON-REENTRANT: module-level state is safe only because a single agent_loop
# runs at a time. Do NOT call render_event() from two concurrent coroutines.

_text_buffer: list[str] = []


# ── Event renderer ────────────────────────────────────────────────────────────

def render_event(event: object, verbose: bool = False) -> None:
    """Handle one AgentEvent. No mutable stream-kind state — block lifecycle events
    drive every transition explicitly."""

    # ── Thinking block ────────────────────────────────────────────────────────
    if isinstance(event, ThinkingStartAgentEvent):
        print(f"\n{_CYAN}💭 thinking{_R}")
        print(_DIM, end="", flush=True)
        return

    if isinstance(event, ThinkingDeltaAgentEvent):
        print(event.delta, end="", flush=True)
        return

    if isinstance(event, ThinkingEndAgentEvent):
        # Reset dim SGR and terminate the line.
        print(_R, flush=True)
        return

    # ── Text block ────────────────────────────────────────────────────────────
    if isinstance(event, TextStartAgentEvent):
        # Nothing to print; buffering begins.
        return

    if isinstance(event, TextDeltaAgentEvent):
        _text_buffer.append(event.delta)
        return

    if isinstance(event, TextEndAgentEvent):
        text = "".join(_text_buffer)
        _text_buffer.clear()
        if text.strip():
            print(f"\n{_BOLD}●{_R}")
            render_markdown(text)
        return

    # ── Tool lifecycle ────────────────────────────────────────────────────────
    if isinstance(event, ToolDeclaredAgentEvent):
        # Flush any open text buffer that preceded the tool call declaration.
        if _text_buffer:
            text = "".join(_text_buffer)
            _text_buffer.clear()
            if text.strip():
                print(f"\n{_BOLD}●{_R}")
                render_markdown(text)
        key = _key_arg(event.name, event.args)
        print(f"\n{cyan('⚙')} {bold(event.name)}{key} {dim('…')}", flush=True)
        return

    if isinstance(event, ToolExecutingAgentEvent):
        # Intentionally silent — the visual gap between ToolDeclared and
        # ToolResult is itself the timing cue for "running".
        return

    if isinstance(event, ToolResultAgentEvent):
        content = event.result.content if isinstance(event.result.content, str) else ""
        size = len(content.encode())
        status = red("✗") if event.result.is_error else green("✓")
        snippet = (
            f"  {red(content[:80])}"
            if event.result.is_error
            else f"  {dim(_fmt_bytes(size))}"
        )
        print(f"  {status}{snippet}")
        return

    if isinstance(event, ToolBlockedAgentEvent):
        print(f"  {yellow('⊘')} {bold(event.name)} blocked: {dim(event.reason)}")
        return

    # ── Turn markers ──────────────────────────────────────────────────────────
    if isinstance(event, TurnStartEvent):
        print(f"\n{dim(f'[Turn {event.turn}]')} ", end="", flush=True)
        return

    if isinstance(event, TurnEndEvent):
        if event.duration_ms:
            print(dim(f"  ⏱ {event.duration_ms / 1000:.1f}s"), flush=True)
        return

    # ── Error / abort / compaction ────────────────────────────────────────────
    if isinstance(event, ErrorAgentEvent):
        _text_buffer.clear()   # guard: abort any open text block before printing the error
        marker = yellow("⟳") if event.retryable else red("✗")
        print(f"\n{marker} {event.error}")
        return

    if isinstance(event, AbortedAgentEvent):
        _text_buffer.clear()   # guard: flush any partial text accumulated before the abort
        print(f"\n{yellow('⚠')} Interrupted")
        return

    if isinstance(event, CompactionAgentEvent):
        if verbose:
            print(dim(f"[compaction] {event.tokens_before} → {event.tokens_after} tokens"))
        return

    if isinstance(event, DoneAgentEvent):
        pass  # footer printed by caller with session-level stats
        return


def _key_arg(name: str, args: dict) -> str:
    MAX = 55
    def clip(s: str) -> str: return s[:MAX] + "…" if len(s) > MAX else s
    if name == "Bash":  return f": {clip(str(args.get('command', '')))}"
    if name == "Read":  return f": {clip(str(args.get('path', '')))}"
    if name == "Write": return f": {clip(str(args.get('path', '')))}"
    if name == "Edit":  return f": {clip(str(args.get('path', '')))}"
    if name == "Grep":  return f": {clip(repr(str(args.get('pattern', ''))))}"
    if name == "Find":  return f": {clip(str(args.get('pattern', '')))}"
    return ""


def _fmt_bytes(n: int) -> str:
    if n < 1024:            return f"{n} B"
    if n < 1024 * 1024:     return f"{n/1024:.1f} KB"
    return f"{n/(1024*1024):.1f} MB"

# ── Markdown renderer ─────────────────────────────────────────────────────────

def render_markdown(text: str) -> None:
    if not text.strip():
        return
    from rich.markdown import Markdown
    print()
    get_console().print(Markdown(text))

# ── Footer ────────────────────────────────────────────────────────────────────

def print_footer(
    model_id: str,
    session_cost: float,
    usage: TokenUsage,
    turns: int,
    ctx_pct: float,
    session_usage: TokenUsage | None = None,
) -> None:
    sep = dim("  ·  ")
    cache_line = f"↓{usage.cache_read:,} read  ↑{usage.cache_write:,} write"
    parts = [
        f"{turns} turn(s)",
        f"{usage.input:,}in / {usage.output:,}out",
        green(f"${session_cost:.4f}"),
        yellow(cache_line),
        f"ctx: {ctx_pct:.0f}%",
    ]
    print(f"\n{dim('─' * 60)}")
    print(dim(f"[{sep.join(parts)}]"))
    if session_usage is not None and (session_usage.cache_read or session_usage.cache_write):
        session_cache = (
            f"  session cache  ↓{session_usage.cache_read:,} read  ↑{session_usage.cache_write:,} write"
        )
        print(dim(session_cache))
