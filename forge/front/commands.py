"""Declarative slash command table shared by every shell.

Layer: front — imports drive for the SessionHandle type. A Command is
(name, help, handler); handlers are synchronous and return a CommandOutcome
the shell interprets (text to print, quit, clear). Handlers that must await
(e.g. /mcp reconnect) return an async `action` thunk the shell awaits and
prints. "clear" means the shell closes the current session and opens a
fresh one — conversation state lives in the log, so a new session IS a
cleared context.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from forge.adapters.jsonl_store import session_summaries
from forge.adapters.mcp.manager import MCPManager
from forge.drive.session import SessionHandle
from forge.front import memory

if TYPE_CHECKING:
    # Builder C owns adapters/skills.py; only the SkillMeta SHAPE (name +
    # description attrs) is used here, so the import stays type-only and this
    # module loads even before that file lands.
    from forge.adapters.skills import SkillMeta


@dataclass(frozen=True)
class CommandContext:
    session: SessionHandle
    model: str
    mcp: MCPManager | None = None
    # cwd: workspace root, used by /remember to locate <cwd>/.agent-forge/memory.md.
    cwd: Path | None = None
    # skills: the skill catalog for /skills — a Sequence[SkillMeta] or a
    # zero-arg renderer returning the formatted catalog text (integrator picks).
    skills: "Sequence[SkillMeta] | Callable[[], str] | None" = None
    # skill_resolver: maps a skill name to its full body text (or None if the
    # name is not a skill). Drives the '/<name> [args]' run-a-skill dispatch.
    skill_resolver: Callable[[str], str | None] | None = None
    # sessions_root: JsonlStore root for /sessions; None hides the listing.
    sessions_root: Path | None = None


@dataclass(frozen=True)
class CommandOutcome:
    text: str = ""
    quit: bool = False
    clear: bool = False
    # Async follow-up for handlers that must await; the shell prints its result.
    action: Callable[[], Awaitable[str]] | None = None


Handler = Callable[[CommandContext, str], CommandOutcome]


@dataclass(frozen=True)
class Command:
    name: str  # without the leading slash
    help: str
    handler: Handler


def _help(ctx: CommandContext, args: str) -> CommandOutcome:
    width = max(len(c.name) for c in COMMANDS)
    return CommandOutcome(
        text="\n".join(f"/{c.name:<{width}}  {c.help}" for c in COMMANDS)
    )


def _status(ctx: CommandContext, args: str) -> CommandOutcome:
    state = ctx.session.state
    usage = state.usage
    return CommandOutcome(
        text="\n".join(
            (
                f"sid: {ctx.session.sid}",
                f"model: {ctx.model}",
                f"turns: {state.turn}",
                f"tokens: {usage.input_tokens} in / {usage.output_tokens} out "
                f"(cache {usage.cache_read_tokens} read / "
                f"{usage.cache_write_tokens} write)",
            )
        )
    )


def _clear(ctx: CommandContext, args: str) -> CommandOutcome:
    return CommandOutcome(text="context cleared: starting a fresh session", clear=True)


def _quit(ctx: CommandContext, args: str) -> CommandOutcome:
    return CommandOutcome(quit=True)


def _mcp_status_text(manager: MCPManager) -> str:
    statuses = manager.status()
    if not statuses:
        return "mcp: no servers configured"
    errors = manager.errors()
    counts = manager.tool_counts()
    width = max(len(name) for name in statuses)
    lines = ["MCP servers:"]
    for name, status in statuses.items():
        line = f"  {name:<{width}}  {status.value:<12} {counts.get(name, 0)} tools"
        if name in errors:
            line += f"  ({errors[name]})"
        lines.append(line)
    return "\n".join(lines)


def _mcp(ctx: CommandContext, args: str) -> CommandOutcome:
    manager = ctx.mcp
    if manager is None:
        return CommandOutcome(
            text="mcp: no servers configured (mcp.toml or --mcp-server)"
        )
    sub, _, rest = args.partition(" ")
    if not sub:
        return CommandOutcome(text=_mcp_status_text(manager))
    if sub == "reconnect":
        name = rest.strip()
        if not name:
            return CommandOutcome(text="usage: /mcp reconnect <name>")
        if name not in manager.status():
            return CommandOutcome(text=f"mcp: unknown server {name!r}")

        async def do_reconnect() -> str:
            # The reconnect just swaps tools inside the shared source; the
            # executor and prompt pick the change up on their next call/build.
            await manager.reconnect(name)
            line = f"{name}: {manager.status()[name].value}"
            error = manager.errors().get(name)
            return f"{line}  ({error})" if error else line

        return CommandOutcome(action=do_reconnect)
    return CommandOutcome(text=f"unknown subcommand: /mcp {sub} (try /mcp)")


def _render_skills(payload: object) -> str:
    """Render a skills payload to '<name> — <description>' lines.

    Accepts a callable renderer (returns formatted text) or a Sequence of
    SkillMeta-shaped objects (have .name / .description). Anything empty or
    unrecognised renders as the no-skills line.
    """
    if payload is None:
        return "no skills found"
    if callable(payload):
        text = payload()
        return text.strip() if text and text.strip() else "no skills found"
    lines = []
    for meta in payload:
        name = getattr(meta, "name", None)
        if not name:
            continue
        desc = getattr(meta, "description", "") or ""
        lines.append(f"{name} — {desc}".rstrip(" —"))
    return "\n".join(lines) if lines else "no skills found"


def _skills(ctx: CommandContext, args: str) -> CommandOutcome:
    return CommandOutcome(text=_render_skills(ctx.skills))


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _sessions(ctx: CommandContext, args: str) -> CommandOutcome:
    """Read-only session list (same store as `forge sessions`)."""
    import time

    root = ctx.sessions_root
    if root is None:
        return CommandOutcome(text="sessions: no sessions root configured")
    all_dirs = args.strip() == "all"
    cwd = None if all_dirs or ctx.cwd is None else str(ctx.cwd)
    rows = session_summaries(root, cwd=cwd)
    if not rows:
        return CommandOutcome(text="no sessions yet")
    now = time.time()
    lines = []
    for r in rows:
        age = _format_age(now - r["updated_at"])
        prompt = r["prompt"] or "(no prompt)"
        lines.append(f"{r['sid'][:12]}  {age:>8}  {prompt}")
    return CommandOutcome(text="\n".join(lines))


def _remember(ctx: CommandContext, args: str) -> CommandOutcome:
    text = args.strip()
    if not text:
        return CommandOutcome(text="usage: /remember <text>")
    cwd = ctx.cwd if ctx.cwd is not None else Path.cwd()
    return CommandOutcome(text=memory.remember(cwd, text))


def _failures(ctx: CommandContext, args: str) -> CommandOutcome:
    """Show the derived failure fold for this directory."""
    from forge.adapters.failures import read_projection, sync_failures

    cwd = ctx.cwd if ctx.cwd is not None else Path.cwd()
    all_dirs = args.strip() == "all"
    if ctx.sessions_root is not None:
        sync_failures(ctx.sessions_root, cwd, all_dirs=all_dirs)
    if all_dirs and ctx.sessions_root is not None:
        from forge.adapters.failures import project_cwds

        chunks: list[str] = []
        for other in project_cwds(ctx.sessions_root, fallback=str(cwd)):
            text = read_projection(Path(other))
            if text:
                chunks.append(f"# {other}\n{text}")
        return CommandOutcome(text="\n\n".join(chunks) if chunks else "no failures recorded")
    text = read_projection(cwd)
    return CommandOutcome(text=text if text else "no failures recorded")


# Delimiter that frames an injected skill body so the model can tell the loaded
# instructions apart from the user's own words.
_SKILL_OPEN = "<skill-instructions>"
_SKILL_CLOSE = "</skill-instructions>"


def _run_skill(ctx: CommandContext, name: str, body: str, args: str) -> CommandOutcome:
    """Submit an augmented user turn: the skill body in a delimited block + args."""
    parts = [
        f"{_SKILL_OPEN} name={name}",
        body.strip(),
        _SKILL_CLOSE,
    ]
    if args.strip():
        parts.append(args.strip())
    prompt = "\n".join(parts)

    async def submit_skill() -> str:
        # submit() is async; awaiting it here keeps the dispatch table sync.
        await ctx.session.submit(prompt)
        return ""

    return CommandOutcome(action=submit_skill)


COMMANDS: tuple[Command, ...] = (
    Command("help", "list available commands", _help),
    Command("status", "show session id, model, turns, and token usage", _status),
    Command("mcp", "show MCP server status; '/mcp reconnect <name>' restores one", _mcp),
    Command("skills", "list available skills; run one with '/<name> [args]'", _skills),
    Command("remember", "save a learning to project memory ('/remember <text>')", _remember),
    Command("sessions", "list recent sessions in this directory ('/sessions all' for every cwd)", _sessions),
    Command("failures", "show derived failure lessons ('/failures all' for every cwd)", _failures),
    Command("clear", "drop the conversation and start a fresh session", _clear),
    Command("quit", "exit the shell", _quit),
)


def banner_commands() -> str:
    """Slash names for the REPL banner, derived from the live table."""
    return "  ".join(f"/{c.name}" for c in COMMANDS)


def dispatch(line: str, ctx: CommandContext) -> CommandOutcome:
    """Resolve one '/name args' line: known command, else known skill, else help.

    A slash token that is not a registered command but IS a known skill name
    (per ctx.skill_resolver) runs that skill — its body is injected into an
    augmented user turn. Tokens that are neither fall through to the unknown
    handler.
    """
    name, _, args = line.strip().lstrip("/").partition(" ")
    args = args.strip()
    for cmd in COMMANDS:
        if cmd.name == name:
            return cmd.handler(ctx, args)
    if ctx.skill_resolver is not None and name:
        body = ctx.skill_resolver(name)
        if body is not None:
            return _run_skill(ctx, name, body, args)
    return CommandOutcome(text=f"unknown command: /{name} (try /help)")
