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

from .context import ContextWindow
from .loop import AgentResult, make_config
from .messages import UserMessage
from .models import DEFAULT_MODEL, MODELS, Model
from .prompts import build_system_prompt
from .renderer import dim, bold, green, red, yellow, render_event, print_footer
from .runner import drive
from .session import (
    append_message, append_metadata, latest_session_id,
    new_id, resume_session, update_memory,
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


# ── Chat session ──────────────────────────────────────────────────────────────

async def run_chat(cfg: ChatConfig) -> None:
    tool_registry = default_registry()

    # Session setup
    session_id: str
    messages = []
    if cfg.continue_session:
        target = cfg.resume_id or latest_session_id(cfg.cwd)
        if not target:
            print(red("No previous session found."))
            return
        resumed = resume_session(target)
        session_id = resumed.session_id
        messages = resumed.messages
        print(green(f"[Session resumed: {session_id[:8]}… {len(messages)} messages]"))
    else:
        session_id = new_id()
        append_metadata(session_id, cfg.model.id, cfg.cwd)

    system_prompt = build_system_prompt(cfg, tool_registry)
    ctx = ContextWindow(model=cfg.model)
    if messages:
        ctx.init_from_existing(messages)

    # Banner
    print(f"\n{bold('agent-forge')} {dim(f'v{__version__}')}")
    print(dim(f"  Model: {cfg.model.id} · {cfg.model.context_window//1000}K ctx · /quit /clear /status"))
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

    def _make_cfg():
        sp_sections = system_prompt.build()
        return make_config(
            model=cfg.model,
            api_key=cfg.api_key,
            system_prompt=sp_sections,
            tool_registry=tool_registry,
            cwd=cfg.cwd,
            thinking=cfg.thinking,
            max_turns=cfg.max_turns,
            max_tokens=cfg.model.max_tokens,
            signal=abort,
        )

    while True:
        try:
            user_input = await ptk_session.prompt_async("> ", bottom_toolbar=_toolbar)
        except (EOFError, KeyboardInterrupt):
            print()
            _save_learnings(messages, cfg.cwd, cfg.verbose)
            break

        user_input = _expand_pastes(user_input.strip(), _paste_store)
        if not user_input:
            continue

        # Slash commands
        if user_input in ("/quit", "/exit", "/q"):
            _save_learnings(messages, cfg.cwd, cfg.verbose)
            break
        if user_input == "/clear":
            messages.clear()
            ctx.clear()
            system_prompt.invalidate_all()
            print(dim("Conversation cleared."))
            continue
        if user_input == "/status":
            tok = ctx.estimate_tokens()
            pct = tok / cfg.model.context_window * 100
            print(f"{bold('── Status ──')}\n"
                  f"  Session: {session_id}\n"
                  f"  Model:   {cfg.model.id}\n"
                  f"  Context: ~{tok} tokens ({pct:.1f}%)\n"
                  f"  Turns:   {ctx.current_turn}  Messages: {len(messages)}")
            continue
        if user_input == "/model":
            print("Available models:")
            for mid in MODELS:
                m = MODELS[mid]
                print(f"  {mid} ({m.context_window//1000}K ctx)")
            new_id_input = (await ptk_session.prompt_async("Enter model id: ", bottom_toolbar=_toolbar)).strip()
            if new_id_input in MODELS:
                cfg.model = MODELS[new_id_input]
                ctx._model = cfg.model
                system_prompt.invalidate_all()
                print(green(f"Switched to {cfg.model.id}"))
            else:
                print(red(f"Unknown model: {new_id_input}"))
            continue

        # User turn
        user_msg = UserMessage(content=user_input)
        messages.append(user_msg)
        append_message(session_id, user_msg)

        # Build initial messages for this turn
        initial_msgs = ctx.build_messages(user_msg)
        abort = asyncio.Event()
        loop_cfg = _make_cfg()   # captures current `abort` via closure

        result: AgentResult | None = None

        try:
            result = await drive(
                loop_cfg, initial_msgs,
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

            # First sync: replace heuristic with real API count *before* the footer
            # so ctx_pct displayed there reflects what the API actually saw.
            ctx.sync_total_tokens(result.usage.input + result.usage.cache_read)
            tok = ctx.estimate_tokens()
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
            # Persist messages — AssistantMessage.usage is self-contained now
            from .messages import AssistantMessage as AM
            for msg in result.messages:
                usage = msg.usage if isinstance(msg, AM) else None
                messages.append(msg)
                append_message(session_id, msg, usage)

            # Advance ContextWindow — receive() resets _synced_total because the
            # window now contains new messages the API hasn't seen yet.
            ctx.receive(
                user_message=user_msg,
                assistant_messages=[m for m in result.messages if not isinstance(m, UserMessage)],
                tool_calls=result.tool_calls,
            )
            # Second sync: re-apply the real count so manage_pressure() uses it
            # rather than falling back to the chars/4 heuristic for the new window.
            ctx.sync_total_tokens(result.usage.input + result.usage.cache_read)

            # Context pressure management
            tier = await ctx.manage_pressure()
            if cfg.verbose and tier.value != "none":
                print(dim(f"[context] pressure tier: {tier.value}"))


# ── Single-prompt (non-interactive) mode ──────────────────────────────────────

async def _run_single_prompt(cfg: ChatConfig, prompt: str) -> None:
    tool_registry = default_registry()
    system_prompt = build_system_prompt(cfg, tool_registry)
    loop_cfg = make_config(
        model=cfg.model,
        api_key=cfg.api_key,
        system_prompt=system_prompt.build(),
        tool_registry=tool_registry,
        cwd=cfg.cwd,
        thinking=cfg.thinking,
        max_turns=cfg.max_turns,
        max_tokens=cfg.model.max_tokens,
    )
    user_msg = UserMessage(content=prompt)
    result = await drive(
        loop_cfg, [user_msg],
        on_event=lambda ev: render_event(ev, cfg.verbose),
    )
    if result:
        ctx_pct = (result.usage.input + result.usage.cache_read) / cfg.model.context_window * 100
        print_footer(cfg.model.id, result.usage.cost, result.usage, result.turns, ctx_pct)


# ── Memory helpers ────────────────────────────────────────────────────────────

def _save_learnings(messages: list, cwd: str, verbose: bool) -> None:
    try:
        learnings = _extract_learnings(messages)
        if learnings:
            update_memory(cwd, learnings, "project")
            if verbose:
                print(dim(f"[memory] Saved {len(learnings)} learning(s)"))
    except Exception:
        pass


def _extract_learnings(messages: list) -> list[str]:
    """Simple heuristic: extract correction signals from user messages."""
    learnings: list[str] = []
    correction_markers = ["don't", "instead of", "use ", "always ", "never "]
    from .messages import UserMessage as UM
    for msg in messages:
        if not isinstance(msg, UM):
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if any(m in content.lower() for m in correction_markers) and len(content) < 200:
            learnings.append(content)
    return learnings[:5]


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
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
