"""Composition root and CLI entry point: one frozen Settings, all env reads.

Layer: front — imports everything; nothing imports it. ALL os.environ
reads in forge live in this module (credentials, model override, sessions
root, cache TTL); every layer below receives values, never reads the
environment. main() is the `forge` console script: subcommand-less default
is the REPL, `forge run -p ...` is the oneshot/eval mode. provider="fake"
wires forge.testing.FakeProvider with a canned script — forge.testing is a
published surface by design, and it is what makes the CLI testable
end-to-end without a network.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform as platform_mod
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge.adapters.jsonl_store import JsonlStore
from forge.adapters.tools import builtin_tools
from forge.adapters.tools.workspace import RootedWorkspace
from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.front import oneshot, repl
from forge.front.render import Renderer
from forge.kernel.types import ModelOutput, TextBlock, Usage
from forge.policy import StandardPolicy
from forge.policy.prompt import environment_section
from forge.ports.asker import Asker
from forge.ports.provider import Provider
from forge.testing import FakeProvider

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TURNS = 40
# Spec section 3.4 canonical location; jsonl_store.default_root() differs —
# wiring picks the canonical one explicitly, as the store module documents.
DEFAULT_SESSIONS_ROOT = Path.home() / ".agent-forge" / "sessions"

FAKE_RESPONSE = "(fake) hello from forge"
_FAKE_SCRIPT_LEN = 64  # generous: the fake REPL survives many turns


class WiringError(Exception):
    """Composition failure the CLI reports as exit code 2 (e.g. no credentials)."""


@dataclass(frozen=True)
class Settings:
    provider: Literal["anthropic", "fake"]
    model: str
    api_key: str | None
    max_turns: int
    ws_root: Path
    sessions_root: Path
    cache_ttl: str | None = None


def load_settings(args: argparse.Namespace) -> Settings:
    # OAuth token wins over the API key (legacy dispatch order); the adapter
    # sniffs the token shape itself, so one field carries both.
    api_key = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get(
        "ANTHROPIC_API_KEY"
    )
    sessions_env = os.environ.get("FORGE_SESSIONS_ROOT")
    return Settings(
        provider=args.provider,
        model=args.model or os.environ.get("FORGE_MODEL") or DEFAULT_MODEL,
        api_key=api_key,
        max_turns=args.max_turns,
        ws_root=Path.cwd(),
        sessions_root=Path(sessions_env) if sessions_env else DEFAULT_SESSIONS_ROOT,
        cache_ttl=os.environ.get("FORGE_CACHE_TTL"),
    )


# --- providers -------------------------------------------------------------------


def fake_script() -> list[ModelOutput]:
    out = ModelOutput(
        blocks=(TextBlock(FAKE_RESPONSE),),
        usage=Usage(input_tokens=24, output_tokens=8),
    )
    return [out] * _FAKE_SCRIPT_LEN


def build_provider(settings: Settings) -> Provider:
    if settings.provider == "fake":
        return FakeProvider(fake_script())
    if not settings.api_key:
        raise WiringError(
            "no credentials: set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN"
        )
    # Lazy import: the anthropic SDK is only required when actually selected,
    # so `forge --provider fake` works in an SDK-free environment.
    from forge.adapters.anthropic import AnthropicProvider

    return AnthropicProvider(settings.api_key, cache_ttl=settings.cache_ttl)


# --- session composition ------------------------------------------------------------


def _environment_facts(ws_root: Path) -> Mapping[str, str]:
    return {
        "working directory": str(ws_root),
        "platform": platform_mod.platform(terse=True),
    }


def build_session(
    settings: Settings,
    *,
    provider: Provider,
    asker: Asker,
    context_tokens: int,
    sid: str | None = None,
) -> SessionHandle:
    ws = RootedWorkspace(settings.ws_root)
    tools = builtin_tools()
    specs = tuple(t.spec for t in tools)
    policy = StandardPolicy(
        model=settings.model,
        context_tokens=context_tokens,
        tools=specs,
        ws_root=ws.root,
        sections=(environment_section(lambda: _environment_facts(ws.root)),),
        max_turns=settings.max_turns,
    )
    sid = sid if sid is not None else uuid.uuid4().hex
    store = JsonlStore(settings.sessions_root, sid)
    executor = ToolExecutor(tools, ws)
    return SessionHandle.open(
        store, provider=provider, executor=executor, policy=policy, asker=asker, sid=sid
    )


# --- entry point ----------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=None, help="model id (env: FORGE_MODEL)")
    parser.add_argument(
        "--provider",
        choices=("anthropic", "fake"),
        default="anthropic",
        help="model provider; 'fake' is the offline scripted provider",
    )
    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="model rounds allowed per user turn",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge", description="forge v2: a coding agent over one event log"
    )
    _add_common(parser)
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="run one turn and exit (the eval mode)")
    _add_common(run)
    run.add_argument("-p", "--prompt", required=True, help="the user prompt")
    run.add_argument(
        "--json",
        action="store_true",
        help="emit the run record as JSON on stdout (rendering goes to stderr)",
    )
    return parser


async def _run_main(settings: Settings, *, prompt: str, json_out: bool) -> int:
    provider = build_provider(settings)
    info = await provider.info(settings.model)
    handle = build_session(
        settings,
        provider=provider,
        asker=oneshot.StaticAsker(allow=False),
        context_tokens=info.context_tokens,
    )
    renderer = Renderer(
        out=sys.stderr if json_out else sys.stdout, pricing=info.pricing
    )
    return await oneshot.run_once(
        handle,
        prompt,
        renderer=renderer,
        json_out=json_out,
        out=sys.stdout,
        pricing=info.pricing,
    )


async def _repl_main(settings: Settings) -> int:
    info = await build_provider(settings).info(settings.model)
    asker = repl.ConsoleAsker()

    async def make_session() -> SessionHandle:
        # A fresh provider per session: /clear gets a clean fake script too.
        return build_session(
            settings,
            provider=build_provider(settings),
            asker=asker,
            context_tokens=info.context_tokens,
        )

    return await repl.run_repl(
        make_session, model=settings.model, pricing=info.pricing
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args)
        if args.cmd == "run":
            return asyncio.run(
                _run_main(settings, prompt=args.prompt, json_out=args.json)
            )
        return asyncio.run(_repl_main(settings))
    except WiringError as exc:
        print(f"forge: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
