"""
chat.py — interactive REPL and CLI entry point (composition root).

Depends on all other modules. Sits at the top of the dependency stack as
the composition root for interactive use: wires loop + context + session +
prompts + tools + renderer into the prompt_toolkit REPL. The CLI entry point
(`agent-forge` script defined in pyproject.toml) lives in main() here.

autonomous.py is a parallel composition root for non-interactive use; the two
share loop/context/tools/prompts/renderer but diverge at the UI/persistence layer.

Owns: run_chat() (REPL), _run_single_prompt() (--prompt flag), main() (CLI),
      paste collapse/expand, /slash commands, end-of-session learning extraction,
      ContextWindow + session wiring (advance + persist after each turn).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

try:
    __version__ = _pkg_version("agent-forge")
except PackageNotFoundError:  # not installed (e.g. running from a source checkout without `pip install -e .`)
    __version__ = "0+unknown"

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style as PtkStyle

from .loop import AgentResult
from .messages import UserMessage
from .models import DEFAULT_MODEL, MODELS, Model
from .prompts import build_system_prompt
from .renderer import dim, bold, green, red, yellow, render_event, print_footer
from .runtime import AgentRuntime
from .session import (
    append_message, append_metadata, latest_session_id,
    list_sessions_for_cwd, new_id, read_session_meta,
    render_session_markdown, resolve_session_spec, resume_session,
    update_memory,
)
from .tools import default_registry

# ── ChatConfig ────────────────────────────────────────────────────────────────

@dataclass
class ChatConfig:
    api_key: str
    model: Model = DEFAULT_MODEL
    cwd: str = "."
    thinking: str = "medium"
    max_turns: int = 100  # Fix 4: raised from 50 to match coding-agent-flow
    verbose: bool = False
    continue_session: bool = False
    resume_id: str | None = None
    custom_system_prompt: str | None = None

# ── Paste handling ────────────────────────────────────────────────────────────

_PASTE_RE = re.compile(r'\[paste #(\d+)[^\]]*\]')
_LARGE_PASTE_LINES = 10
_LARGE_PASTE_CHARS = 1000


def _make_paste_bindings(store: dict[int, str], counter: list[int]) -> KeyBindings:
    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste)
    def _on_paste(event) -> None:
        data: str = event.data
        lines = data.split("\n")
        if len(lines) > _LARGE_PASTE_LINES or len(data) > _LARGE_PASTE_CHARS:
            counter[0] += 1
            pid = counter[0]
            store[pid] = data
            if len(lines) > _LARGE_PASTE_LINES:
                marker = f"[paste #{pid} +{len(lines)} lines]"
            else:
                marker = f"[paste #{pid} {len(data)} chars]"
            event.current_buffer.insert_text(marker)
        else:
            event.current_buffer.insert_text(data)

    return kb


def _expand_pastes(text: str, store: dict[int, str]) -> str:
    def _sub(m: re.Match) -> str:
        pid = int(m.group(1))
        return store.pop(pid, m.group(0))
    return _PASTE_RE.sub(_sub, text)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_age(ts_seconds: float) -> str:
    """Human-friendly age: '3h ago', '2d ago', 'just now'."""
    import time as _t
    delta = max(0, _t.time() - ts_seconds)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


# ── Chat session ──────────────────────────────────────────────────────────────

async def run_chat(cfg: ChatConfig) -> None:
    tool_registry = default_registry()

    # Session setup
    session_id: str
    messages = []
    # --resume <X> implies resume mode (no need to also pass -c).
    want_resume = cfg.continue_session or bool(cfg.resume_id)
    if want_resume:
        if cfg.resume_id:
            target = resolve_session_spec(cfg.resume_id, cfg.cwd)
            if target is None:
                print(red(
                    f"No session matches '{cfg.resume_id}'. "
                    f"Try `agent-forge sessions ls` (or --all) to see options."
                ))
                return
        else:
            target = latest_session_id(cfg.cwd)
            if not target:
                print(red("No previous session found."))
                return
        resumed = resume_session(target)
        session_id = resumed.session_id
        messages = resumed.messages
        meta = read_session_meta(session_id)
        title = (meta.title if meta and meta.title else "(untitled)")
        age = _format_age(meta.last_modified) if meta else "?"
        print(green(
            f"[Resumed] \"{title}\" · {len(messages)} messages · "
            f"{age} · {session_id[:8]}…"
        ))
    else:
        session_id = new_id()
        append_metadata(session_id, cfg.model.id, cfg.cwd)

    system_prompt = build_system_prompt(cfg, tool_registry)
    runtime = AgentRuntime(
        model=cfg.model,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        cwd=cfg.cwd,
        api_key=cfg.api_key,
        thinking=cfg.thinking,
        max_turns=cfg.max_turns,
        max_tokens=cfg.model.max_tokens,
    )
    if messages:
        runtime.init_messages(messages)

    # Banner
    print(f"\n{bold('agent-forge')} {dim(f'v{__version__}')}")
    print(dim(f"  Model: {cfg.model.id} · {cfg.model.context_window//1000}K ctx · /quit /clear /status /remember"))
    print()

    # prompt_toolkit session — persistent history in ~/.agent-forge-history
    history_file = Path.home() / ".agent-forge-history"
    ptk_style = PtkStyle.from_dict({"prompt": "ansicyan bold"})
    _paste_store: dict[int, str] = {}
    _paste_counter: list[int] = [0]
    ptk_session: PromptSession = PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
        style=ptk_style,
        mouse_support=False,
        key_bindings=_make_paste_bindings(_paste_store, _paste_counter),
    )

    session_cost: float = 0.0
    session_turns: int = 0
    session_ctx_pct: float = 0.0
    session_cache_read: int = 0
    session_cache_write: int = 0

    def _toolbar() -> str:
        parts = [
            cfg.model.id,
            f"turns: {session_turns}",
            f"ctx: {session_ctx_pct:.0f}%",
            f"↓{session_cache_read:,} cache_read",
            f"↑{session_cache_write:,} cache_write",
        ]
        return "  │  ".join(parts)

    abort: asyncio.Event | None = None
    _exited_gracefully = False

    while True:
        try:
            user_input = await ptk_session.prompt_async("> ", bottom_toolbar=_toolbar)
        except (EOFError, KeyboardInterrupt):
            print()
            _exited_gracefully = True
            break

        user_input = _expand_pastes(user_input.strip(), _paste_store)
        if not user_input:
            continue

        # Slash commands
        if user_input in ("/quit", "/exit", "/q"):
            _exited_gracefully = True
            break
        if user_input.startswith("/remember "):
            note = user_input[len("/remember "):].strip()
            if not note:
                print(red("Usage: /remember <text to save to memory>"))
            else:
                try:
                    update_memory(cfg.cwd, [note], "project")
                    print(green(f"[memory] Saved: {note[:60]}…" if len(note) > 60 else f"[memory] Saved: {note}"))
                except Exception as exc:
                    print(red(f"[memory] Save failed: {exc}"))
            continue
        if user_input == "/sessions":
            metas = list_sessions_for_cwd(cfg.cwd)
            if not metas:
                print(dim("No sessions for this cwd yet."))
                continue
            print(bold("Sessions in this directory (newest first):"))
            for i, m in enumerate(metas[:20], start=1):
                marker = " ← current" if m.session_id == session_id else ""
                title = m.title or dim("(untitled)")
                age = _format_age(m.last_modified)
                # 2-col layout: idx + title + age + sid prefix
                print(f"  {i:>2}  {title:<60}  {age:<10}  {dim(m.session_id[:8])}{marker}")
            print(dim("  Switch to one:  /resume <n|id>"))
            continue
        if user_input.startswith("/resume"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print(red("Usage: /resume <n|id>   (see /sessions for the list)"))
                continue
            target = resolve_session_spec(parts[1], cfg.cwd)
            if target is None:
                print(red(f"No session matches '{parts[1]}'. Try /sessions to see options."))
                continue
            if target == session_id:
                print(dim("Already on that session."))
                continue
            resumed = resume_session(target)
            # Swap state in place — keep the `messages` list reference stable.
            messages.clear()
            messages.extend(resumed.messages)
            session_id = target
            runtime.clear()
            if messages:
                runtime.init_messages(messages)
            # Reset toolbar / footer counters for the new session.
            session_cost = 0.0
            session_turns = 0
            session_ctx_pct = 0.0
            session_cache_read = 0
            session_cache_write = 0
            meta = read_session_meta(session_id)
            title = (meta.title if meta and meta.title else "(untitled)")
            age = _format_age(meta.last_modified) if meta else "?"
            print(green(
                f"[Switched] \"{title}\" · {len(messages)} messages · "
                f"{age} · {session_id[:8]}…"
            ))
            continue
        if user_input == "/clear":
            messages.clear()
            runtime.clear()
            print(dim("Conversation cleared."))
            continue
        if user_input == "/status":
            tok = runtime.context.estimate_tokens()
            pct = tok / cfg.model.context_window * 100
            print(f"{bold('── Status ──')}\n"
                  f"  Session: {session_id}\n"
                  f"  Model:   {cfg.model.id}\n"
                  f"  Context: ~{tok} tokens ({pct:.1f}%)\n"
                  f"  Turns:   {runtime.context.current_turn}  Messages: {len(messages)}")
            continue
        if user_input == "/model":
            print("Available models:")
            for mid in MODELS:
                m = MODELS[mid]
                print(f"  {mid} ({m.context_window//1000}K ctx)")
            new_id_input = (await ptk_session.prompt_async("Enter model id: ", bottom_toolbar=_toolbar)).strip()
            if new_id_input in MODELS:
                cfg.model = MODELS[new_id_input]
                runtime.model = cfg.model
                runtime.context._model = cfg.model
                system_prompt.invalidate_all()
                print(green(f"Switched to {cfg.model.id}"))
            else:
                print(red(f"Unknown model: {new_id_input}"))
            continue

        # User turn
        user_msg = UserMessage(content=user_input)
        messages.append(user_msg)
        append_message(session_id, user_msg)

        abort = asyncio.Event()
        result: AgentResult | None = None

        try:
            result = await runtime.run_turn(
                user_msg, signal=abort,
                on_event=lambda ev: render_event(ev, cfg.verbose),
            )
        except KeyboardInterrupt:
            print(f"\n{yellow('Interrupted')}")
            abort.set()

        if result:
            session_cost += result.usage.cost
            session_turns += result.turns
            session_cache_read += result.usage.cache_read
            session_cache_write += result.usage.cache_write

            tok = runtime.context.estimate_tokens()
            session_ctx_pct = tok / cfg.model.context_window * 100
            from .messages import TokenUsage as TU
            session_usage = TU(
                input=0, output=0,
                cache_read=session_cache_read,
                cache_write=session_cache_write,
            )
            print_footer(cfg.model.id, session_cost, result.usage, result.turns, session_ctx_pct, session_usage)
        print()  # blank line after footer

        if result:
            # Persist messages — AssistantMessage.usage is self-contained now.
            # ContextWindow advance + pressure management happened inside run_turn().
            from .messages import AssistantMessage as AM
            for msg in result.messages:
                usage = msg.usage if isinstance(msg, AM) else None
                messages.append(msg)
                append_message(session_id, msg, usage)

            if cfg.verbose:
                tier = runtime.pressure_tier()
                if tier.value != "none":
                    print(dim(f"[context] pressure tier: {tier.value}"))



# ── Single-prompt (non-interactive) mode ──────────────────────────────────────

async def _run_single_prompt(cfg: ChatConfig, prompt: str) -> None:
    tool_registry = default_registry()
    system_prompt = build_system_prompt(cfg, tool_registry)
    runtime = AgentRuntime(
        model=cfg.model,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        cwd=cfg.cwd,
        api_key=cfg.api_key,
        thinking=cfg.thinking,
        max_turns=cfg.max_turns,
        max_tokens=cfg.model.max_tokens,
    )
    user_msg = UserMessage(content=prompt)
    result = await runtime.run_turn(
        user_msg,
        on_event=lambda ev: render_event(ev, cfg.verbose),
    )
    if result:
        ctx_pct = (result.usage.input + result.usage.cache_read) / cfg.model.context_window * 100
        print_footer(cfg.model.id, result.usage.cost, result.usage, result.turns, ctx_pct)


# ── Entry point ───────────────────────────────────────────────────────────────

def _run_sessions_subcommand(argv: list[str]) -> int:
    """`agent-forge sessions ls [--all]` — list persisted sessions.

    No API key required. Reads JSONL log files only. Returns process exit code.
    """
    import argparse

    p = argparse.ArgumentParser(prog="agent-forge sessions")
    sub = p.add_subparsers(dest="action", required=True)
    ls = sub.add_parser("ls", help="List sessions")
    ls.add_argument("--all", action="store_true", help="All cwds, not just the current one")
    ls.add_argument("--cwd", default=os.getcwd())
    show = sub.add_parser("show", help="Print a session as a markdown transcript")
    show.add_argument("spec", help="Session: 1-based index in current cwd, or sid prefix")
    show.add_argument("--cwd", default=os.getcwd())
    args = p.parse_args(argv)

    if args.action == "ls":
        if args.all:
            from .session import list_sessions
            metas = []
            for sid, _ in list_sessions():
                m = read_session_meta(sid)
                if m is not None:
                    metas.append(m)
        else:
            metas = list_sessions_for_cwd(args.cwd)

        if not metas:
            scope = "any cwd" if args.all else args.cwd
            print(dim(f"No sessions found for {scope}."))
            return 0

        print(bold(f"{'#':>3}  {'TITLE':<60}  {'AGE':<10}  {'ID':<10}  CWD"))
        for i, m in enumerate(metas[:50], start=1):
            title = m.title or "(untitled)"
            age = _format_age(m.last_modified)
            cwd_disp = m.cwd if args.all else ""
            print(f"{i:>3}  {title:<60}  {age:<10}  {m.session_id[:8]:<10}  {cwd_disp}")
        return 0

    if args.action == "show":
        sid = resolve_session_spec(args.spec, args.cwd)
        if sid is None:
            print(red(f"No session matches '{args.spec}'. Try `agent-forge sessions ls`."), file=sys.stderr)
            return 1
        md = render_session_markdown(sid)
        if md is None:
            print(red(f"Session {sid} could not be read."), file=sys.stderr)
            return 1
        print(md)
        return 0

    p.error(f"unknown action: {args.action}")
    return 2  # unreachable; keeps mypy happy


def main() -> None:
    # Sub-command dispatch (no API key required for `sessions`).
    if len(sys.argv) > 1 and sys.argv[1] == "sessions":
        sys.exit(_run_sessions_subcommand(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "wiki":
        # Deprecation shim (one release). The wiki was extracted to a skill
        # at .claude/skills/agent-forge-wiki/ in agent-forge 1.1; the CLI
        # subcommand goes away in 1.2.
        print(
            red("agent-forge wiki has moved to a skill."),
            file=sys.stderr,
        )
        print(
            dim(
                "Run instead:\n"
                "  python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py "
                + " ".join(sys.argv[2:])
            ),
            file=sys.stderr,
        )
        sys.exit(2)

    import argparse

    parser = argparse.ArgumentParser(prog="agent-forge", description="Minimal coding agent")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--thinking", choices=["off", "adaptive", "low", "medium", "high"], default="medium")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--continue", dest="continue_session", action="store_true")
    parser.add_argument("--resume", dest="resume_id")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--prompt", help="Run a single prompt (non-interactive)")
    parser.add_argument(
        "--debug-stream",
        action="store_true",
        help="Log raw provider stream events with monotonic timestamps to stderr (diagnostic).",
    )
    args = parser.parse_args()

    if args.debug_stream:
        os.environ["AGENT_FORGE_DEBUG_STREAM"] = "1"

    api_key = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )
    if not api_key:
        print(red("Error: set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY"), file=sys.stderr)
        sys.exit(1)

    try:
        model = Model.from_id(args.model)
    except ValueError as e:
        print(red(str(e)), file=sys.stderr)
        sys.exit(1)

    cfg = ChatConfig(
        api_key=api_key,
        model=model,
        cwd=args.cwd,
        thinking=args.thinking,
        verbose=args.verbose,
        continue_session=args.continue_session,
        resume_id=args.resume_id,
    )

    if args.prompt:
        asyncio.run(_run_single_prompt(cfg, args.prompt))
    else:
        asyncio.run(run_chat(cfg))


if __name__ == "__main__":
    main()
