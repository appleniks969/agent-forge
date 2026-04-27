"""
renderer.py — ANSI helpers, event renderer, markdown printer, footer.

Depends on loop (AgentEvent types) and provider (TokenUsage). Presentation-only
leaf of the composition layer — zero business logic, zero file I/O. Both chat.py
and autonomous.py import render_event(); neither needs to know the ANSI details.

Owns: dim/bold/green/red/yellow/cyan helpers, render_event() (handles all 12
      AgentEvent branches), render_markdown(), print_footer(), get_console()
      (Rich singleton).
"""
from __future__ import annotations

from .loop import (
    AbortedAgentEvent, DoneAgentEvent, ErrorAgentEvent, TextDeltaAgentEvent,
    ThinkingDeltaAgentEvent, ToolCallEndAgentEvent, ToolCallStartAgentEvent,
    ToolResultAgentEvent, TurnEndEvent, TurnStartEvent,
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

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ── Rich console singleton ────────────────────────────────────────────────────

_rich_console = None

def get_console():
    global _rich_console
    if _rich_console is None:
        from rich.console import Console
        _rich_console = Console()
    return _rich_console

# ── Event renderer ────────────────────────────────────────────────────────────

def render_event(event: object, verbose: bool = False) -> None:
    if isinstance(event, TurnStartEvent):
        print(f"\n{dim(f'[Turn {event.turn}]')} ", end="", flush=True)
    elif isinstance(event, TextDeltaAgentEvent):
        pass  # buffered by caller; rendered as markdown via render_markdown
    elif isinstance(event, ThinkingDeltaAgentEvent):
        pass  # buffered and flushed as a summary by callers (chat.py / autonomous.py)
    elif isinstance(event, ToolCallStartAgentEvent):
        print(f"\n{cyan('⚙')} {bold(event.name)}", end="", flush=True)
    elif isinstance(event, ToolCallEndAgentEvent):
        key = _key_arg(event.name, event.args)
        print(f"{key} {dim('…')}", flush=True)
    elif isinstance(event, ToolResultAgentEvent):
        size = len(event.result.content.encode())
        status = red("✗") if event.result.is_error else green("✓")
        snippet = (
            f"  {red(event.result.content[:80])}"
            if event.result.is_error
            else f"  {dim(_fmt_bytes(size))}"
        )
        print(f"  {status}{snippet}")
    elif isinstance(event, ErrorAgentEvent):
        marker = yellow("⟳") if event.retryable else red("✗")
        print(f"\n{marker} {event.error}")
    elif isinstance(event, AbortedAgentEvent):
        print(f"\n{yellow('⚠')} Interrupted")
    elif isinstance(event, TurnEndEvent):
        if event.duration_ms:
            print(dim(f"  ⏱ {event.duration_ms / 1000:.1f}s"), flush=True)
    elif isinstance(event, DoneAgentEvent):
        pass  # footer printed by caller with session-level stats


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
    from rich.console import Console
    from rich.markdown import Markdown
    print()
    Console().print(Markdown(text))

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
    # Per-turn cache stats
    cache_line = (
        f"↓{usage.cache_read:,} read  ↑{usage.cache_write:,} write"
    )
    parts = [
        f"{turns} turn(s)",
        f"{usage.input:,}in / {usage.output:,}out",
        green(f"${session_cost:.4f}"),
        yellow(cache_line),
        f"ctx: {ctx_pct:.0f}%",
    ]
    print(f"\n{dim('─' * 60)}")
    print(dim(f"[{sep.join(parts)}]"))
    # Session-level cumulative cache line (shown whenever there are 2+ turns)
    if session_usage is not None and (session_usage.cache_read or session_usage.cache_write):
        session_cache = (
            f"  session cache  ↓{session_usage.cache_read:,} read  ↑{session_usage.cache_write:,} write"
        )
        print(dim(session_cache))
