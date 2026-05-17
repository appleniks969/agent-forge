"""
chat.py — interactive REPL and CLI entry point (composition root).

Depends on all other modules. Sits at the top of the dependency stack as
the sole composition root: wires loop + context + session + prompts + tools +
renderer into the prompt_toolkit REPL. The CLI entry point (`agent-forge`
script defined in pyproject.toml) lives in main() here.

Owns: run_chat() (REPL), _run_single_prompt() (--prompt flag), main() (CLI),
      paste collapse/expand, /slash commands, end-of-session learning extraction,
      ContextWindow + session wiring (advance + persist after each turn).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass, field
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
from .mcp import MCPServerConfig, load_mcp_configs, parse_mcp_server_spec
from .messages import UserMessage
from .models import DEFAULT_MODEL, MODELS, Model
from .prompts import build_chat_prompt_async
from .renderer import dim, bold, cyan, green, red, yellow, render_event, print_footer
from .runtime import AgentRuntime, build_runtime_with_mcp
from .session import (
    append_message, append_metadata, latest_session_id,
    list_sessions_for_cwd, new_id, read_session_meta, redact_secrets,
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
    # ── MCP (Phase H) ──────────────────────────────────────────────────
    # mcp_enabled: when True, load server configs from
    #   ~/.agent-forge/mcp.toml + <cwd>/.agent-forge/mcp.toml at startup
    #   and merge with mcp_servers. When False (--no-mcp), skip the load
    #   entirely — even ad-hoc --mcp-server flags are honoured (they go
    #   through mcp_servers, not the file loader).
    # mcp_servers: ad-hoc configs from repeated --mcp-server CLI flags;
    #   appended after the TOML-loaded list, last write wins by name.
    mcp_enabled: bool = True
    mcp_servers: list = field(default_factory=list)   # list[MCPServerConfig]

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

# ── MCP composition helpers (Phase H) ─────────────────────────────────────────

def _resolve_mcp_configs(cfg: ChatConfig) -> list[MCPServerConfig]:
    """Compute the final list of MCP server configs for this session.

    Sources, merged by name (last write wins so CLI overrides files):
      1. Global file: ``~/.agent-forge/mcp.toml``       (if mcp_enabled)
      2. Project file: ``<cwd>/.agent-forge/mcp.toml``  (if mcp_enabled)
      3. ``--mcp-server`` CLI flags                     (always)

    Returns ``[]`` when MCP is disabled AND no ``--mcp-server`` flags
    were given — `build_runtime_with_mcp` interprets that as "no manager".
    """
    by_name: dict[str, MCPServerConfig] = {}
    if cfg.mcp_enabled:
        for c in load_mcp_configs(cfg.cwd):
            by_name[c.name] = c
    for c in cfg.mcp_servers:
        by_name[c.name] = c
    return list(by_name.values())


def _format_mcp_status(runtime: AgentRuntime) -> str:
    """Render the `/mcp` slash command output for the current runtime."""
    mgr = runtime.mcp_manager
    if mgr is None:
        return dim("MCP is not enabled. Use --mcp or --mcp-server, or add ~/.agent-forge/mcp.toml")
    if not mgr.clients:
        return dim("MCP is enabled but no servers are configured.")
    lines = [bold("MCP servers:")]
    for client in mgr.clients:
        status = client.status.value
        coloured = {
            "connected": green(status),
            "failed": red(status),
            "closed": yellow(status),
            "connecting": yellow(status),
            "disconnected": dim(status),
        }.get(status, status)
        tool_count = len(client.tools())
        err = f"  ({client.error})" if client.error else ""
        lines.append(
            f"  {cyan(client.config.name):20}  {coloured}  "
            f"{dim(f'{tool_count} tool{'s' if tool_count != 1 else ''}')}{err}"
        )
    return "\n".join(lines)


async def _handle_mcp_command(
    user_input: str,
    runtime: AgentRuntime,
    tool_registry,
) -> None:
    """Dispatch ``/mcp [subcommand]``.

    Subcommands:
        /mcp                 — show server status
        /mcp tools           — list MCP tools currently registered
        /mcp reconnect       — reconnect every server
        /mcp reconnect <name> — reconnect one named server
    """
    parts = user_input.split(maxsplit=2)
    sub = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    mgr = runtime.mcp_manager

    if sub == "":
        print(_format_mcp_status(runtime))
        return

    if sub == "tools":
        if mgr is None or not mgr.tools():
            print(dim("No MCP tools registered."))
            return
        print(bold("MCP tools:"))
        for t in mgr.tools():
            print(f"  {cyan(t.name):30}  {dim(t.description)}")
        return

    if sub == "reconnect":
        if mgr is None:
            print(red("MCP is not enabled."))
            return
        if arg:
            print(dim(f"[mcp] reconnecting {arg}…"))
            ok = await mgr.reconnect(arg)
            tool_registry.replace_mcp_tools(mgr.tools())
            print((green if ok else red)(f"[mcp] {arg}: {'connected' if ok else 'failed'}"))
        else:
            print(dim("[mcp] reconnecting all servers…"))
            for client in mgr.clients:
                await mgr.reconnect(client.config.name)
            tool_registry.replace_mcp_tools(mgr.tools())
            print(_format_mcp_status(runtime))
        # The MCP_TOOLS section sits in cache group 1 (session-stable). After
        # a reconnect the underlying tool set may have changed, so we drop
        # the cached resolution and let the next turn re-render. Group 0
        # (identity/tools/guidelines) is untouched, preserving its cache.
        runtime.system_prompt.invalidate_session()
        return

    print(red(f"Unknown /mcp subcommand: {sub!r}. Try /mcp, /mcp tools, /mcp reconnect [name]"))


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

    system_prompt = await build_chat_prompt_async(cfg, tool_registry)
    mcp_configs = _resolve_mcp_configs(cfg)

    def _print_mcp_status(name: str, status: str, error: str | None) -> None:
        if status == "connected":
            print(dim(f"  [mcp] {name}: {green('connected')}"))
        else:
            err = f" — {error}" if error else ""
            print(dim(f"  [mcp] {name}: {red(status)}{err}"))

    runtime = await build_runtime_with_mcp(
        model=cfg.model,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        cwd=cfg.cwd,
        mcp_configs=mcp_configs,
        api_key=cfg.api_key,
        thinking=cfg.thinking,
        max_turns=cfg.max_turns,
        max_tokens=cfg.model.max_tokens,
        on_status=_print_mcp_status if mcp_configs else None,
    )
    if messages:
        runtime.init_messages(messages)

    try:
        # Banner
        print(f"\n{bold('agent-forge')} {dim(f'v{__version__}')}")
        print(dim(f"  Model: {cfg.model.id} · {cfg.model.context_window//1000}K ctx · /quit /clear /status /remember /mcp"))
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
                    runtime.set_model(cfg.model)
                    print(green(f"Switched to {cfg.model.id}"))
                else:
                    print(red(f"Unknown model: {new_id_input}"))
                continue
            if user_input == "/mcp" or user_input.startswith("/mcp "):
                await _handle_mcp_command(user_input, runtime, tool_registry)
                continue

            # User turn
            user_msg = UserMessage(content=user_input)
            messages.append(user_msg)
            append_message(session_id, user_msg, redactor=redact_secrets)

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
                    append_message(session_id, msg, usage, redactor=redact_secrets)

                if cfg.verbose:
                    tier = runtime.pressure_tier()
                    if tier.value != "none":
                        print(dim(f"[context] pressure tier: {tier.value}"))
    finally:
        await runtime.aclose()



# ── Single-prompt (non-interactive) mode ──────────────────────────────────────

async def _run_single_prompt(cfg: ChatConfig, prompt: str) -> None:
    tool_registry = default_registry()
    system_prompt = await build_chat_prompt_async(cfg, tool_registry)
    mcp_configs = _resolve_mcp_configs(cfg)
    runtime = await build_runtime_with_mcp(
        model=cfg.model,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        cwd=cfg.cwd,
        mcp_configs=mcp_configs,
        api_key=cfg.api_key,
        thinking=cfg.thinking,
        max_turns=cfg.max_turns,
        max_tokens=cfg.model.max_tokens,
    )
    async with runtime:
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
    # ── MCP (Phase H) ──────────────────────────────────────────────────
    # Default behavior: MCP IS enabled but only loads servers from
    # ~/.agent-forge/mcp.toml and <cwd>/.agent-forge/mcp.toml. If neither
    # file exists, no servers are loaded — silent no-op. `--no-mcp` forces
    # the file loader to be skipped; ad-hoc `--mcp-server` still works.
    mcp_group = parser.add_mutually_exclusive_group()
    mcp_group.add_argument(
        "--mcp", dest="mcp_enabled", action="store_true", default=True,
        help="Load MCP server configs from ~/.agent-forge/mcp.toml (default).",
    )
    mcp_group.add_argument(
        "--no-mcp", dest="mcp_enabled", action="store_false",
        help="Skip the MCP config-file loader. --mcp-server flags still work.",
    )
    parser.add_argument(
        "--mcp-server", action="append", default=[], metavar="SPEC",
        help="Add an ad-hoc MCP server. Format: 'name=command [args...]'. Repeatable.",
    )
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

    # Parse --mcp-server SPECs (repeatable) into MCPServerConfig list
    mcp_servers = []
    for spec in args.mcp_server:
        try:
            mcp_servers.append(parse_mcp_server_spec(spec))
        except ValueError as exc:
            print(red(f"--mcp-server: {exc}"), file=sys.stderr)
            sys.exit(1)

    cfg = ChatConfig(
        api_key=api_key,
        model=model,
        cwd=args.cwd,
        thinking=args.thinking,
        verbose=args.verbose,
        continue_session=args.continue_session,
        resume_id=args.resume_id,
        mcp_enabled=args.mcp_enabled,
        mcp_servers=mcp_servers,
    )

    if args.prompt:
        asyncio.run(_run_single_prompt(cfg, args.prompt))
    else:
        asyncio.run(run_chat(cfg))


if __name__ == "__main__":
    main()
