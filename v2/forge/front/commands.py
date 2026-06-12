"""Declarative slash command table shared by every shell.

Layer: front — imports drive for the SessionHandle type. A Command is
(name, help, handler); handlers are synchronous and return a CommandOutcome
the shell interprets (text to print, quit, clear). "clear" means the shell
closes the current session and opens a fresh one — conversation state lives
in the log, so a new session IS a cleared context.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from forge.drive.session import SessionHandle


@dataclass(frozen=True)
class CommandContext:
    session: SessionHandle
    model: str


@dataclass(frozen=True)
class CommandOutcome:
    text: str = ""
    quit: bool = False
    clear: bool = False


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


COMMANDS: tuple[Command, ...] = (
    Command("help", "list available commands", _help),
    Command("status", "show session id, model, turns, and token usage", _status),
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
