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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from forge.adapters.mcp.manager import MCPManager
from forge.drive.session import SessionHandle


@dataclass(frozen=True)
class CommandContext:
    session: SessionHandle
    model: str
    mcp: MCPManager | None = None


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


COMMANDS: tuple[Command, ...] = (
    Command("help", "list available commands", _help),
    Command("status", "show session id, model, turns, and token usage", _status),
    Command("mcp", "show MCP server status; '/mcp reconnect <name>' restores one", _mcp),
    Command("clear", "drop the conversation and start a fresh session", _clear),
    Command("quit", "exit the shell", _quit),
)


def dispatch(line: str, ctx: CommandContext) -> CommandOutcome:
    """Resolve one '/name args' line against the table; unknown names get help."""
    name, _, args = line.strip().lstrip("/").partition(" ")
    for cmd in COMMANDS:
        if cmd.name == name:
            return cmd.handler(ctx, args.strip())
    return CommandOutcome(text=f"unknown command: /{name} (try /help)")
