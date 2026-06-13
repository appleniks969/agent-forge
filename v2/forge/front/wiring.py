"""Composition root and CLI entry point: one frozen Settings, all env reads.

Layer: front — imports everything; nothing imports it. ALL os.environ
reads in forge live in this module (credentials, model override, sessions
root, cache TTL); every layer below receives values, never reads the
environment. main() is the `forge` console script: subcommand-less default
is the REPL, `forge run -p ...` is the oneshot/eval mode. provider="fake"
wires forge.testing.FakeProvider with a canned script — forge.testing is a
published surface by design, and it is what makes the CLI testable
end-to-end without a network.

MCP composition also lives here: configs come from mcp.toml plus repeatable
--mcp-server flags (--no-mcp skips the files), and ONE ToolSource over
builtins + the manager's live view feeds both the ToolExecutor and the
policy's tools supplier — so a reconnect's tool swap reaches the executor
and the prompt without re-wiring. The manager's aclose is chained after the
session ends, so no child process outlives the CLI.
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
from forge.adapters.mcp.manager import (
    MCPManager,
    MCPServerConfig,
    load_mcp_configs,
    parse_mcp_server_spec,
)
from forge.adapters.skills import SkillTool, discover_skills, resolve_skill
from forge.adapters.tools import builtin_tools
from forge.adapters.tools.workspace import RootedWorkspace
from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.front import oneshot, repl
from forge.front.orient import (
    agents_doc_supplier,
    memory_supplier,
    repo_map_supplier,
    skills_index_supplier,
)
from forge.front.render import Renderer
from forge.kernel.types import ModelOutput, TextBlock, Usage
from forge.policy import StandardPolicy
from forge.policy.prompt import (
    agents_doc_section,
    environment_section,
    memory_section,
    repo_map_section,
    skills_section,
)
from forge.ports.asker import Asker
from forge.ports.provider import Provider
from forge.ports.source import StaticToolSource, ToolSource
from forge.ports.tool import Tool
from forge.testing import FakeProvider

DEFAULT_MODEL = "claude-sonnet-4-6"
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
    mcp_configs: tuple[MCPServerConfig, ...] = ()


def _resolve_mcp_configs(args: argparse.Namespace) -> tuple[MCPServerConfig, ...]:
    """mcp.toml entries (project over global) unless --no-mcp; --mcp-server
    specs apply either way and override file entries by name (last one wins)."""
    by_name: dict[str, MCPServerConfig] = {}
    if not args.no_mcp:
        for cfg in load_mcp_configs(Path.cwd()):
            by_name[cfg.name] = cfg
    for spec in args.mcp_server or ():
        try:
            cfg = parse_mcp_server_spec(spec)
        except ValueError as exc:
            raise WiringError(str(exc)) from exc
        by_name[cfg.name] = cfg
    return tuple(by_name.values())


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
        mcp_configs=_resolve_mcp_configs(args),
    )


# --- skill roots -------------------------------------------------------------------


def skill_roots(cwd: Path) -> tuple[Path, ...]:
    """Skill search roots in precedence order — PROJECT before GLOBAL.

    discover_skills/resolve_skill de-dup by name with the EARLIEST root winning,
    so listing project roots first lets a workspace skill override a global one.
    Both the `.claude/skills` (wider-ecosystem layout) and `.agent-forge/skills`
    (forge-native layout) directories are scanned at each scope. This is the only
    place home/env is read for skills — the layer law keeps that read in front."""
    home = Path.home()
    return (
        cwd / ".claude" / "skills",
        cwd / ".agent-forge" / "skills",
        home / ".claude" / "skills",
        home / ".agent-forge" / "skills",
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


# --- MCP composition -----------------------------------------------------------


class CompositeToolSource:
    """First-match-wins union of sources. Children's generations are monotonic
    counters, so their sum is a valid change token for the union."""

    def __init__(self, sources: Sequence[ToolSource]) -> None:
        self._sources = tuple(sources)

    @property
    def generation(self) -> int:
        return sum(s.generation for s in self._sources)

    def get(self, name: str) -> Tool | None:
        for source in self._sources:
            tool = source.get(name)
            if tool is not None:
                return tool
        return None

    def all(self) -> tuple[Tool, ...]:
        by_name: dict[str, Tool] = {}
        for source in self._sources:
            for tool in source.all():
                by_name.setdefault(tool.spec.name, tool)
        return tuple(by_name.values())


def build_tool_source(
    manager: MCPManager | None, roots: Sequence[Path] = ()
) -> ToolSource:
    """ONE source feeds the executor and the prompt's tools supplier; the
    manager's live view means a reconnect refreshes both automatically. The
    SkillTool (READ_PATH: parallel-safe, no Ask) is a builtin alongside the
    file tools, so it shows up in the prompt's tools section and is callable
    from the parallel batch like any other read tool."""
    base = StaticToolSource((*builtin_tools(), SkillTool(roots)))
    if manager is None:
        return base
    return CompositeToolSource((base, manager.source()))


async def connect_mcp(settings: Settings) -> MCPManager | None:
    """Auto-enable: a manager exists iff any config resolved; connect failures
    surface per-server via /mcp, never as a startup crash."""
    if not settings.mcp_configs:
        return None
    manager = MCPManager(settings.mcp_configs)
    await manager.connect_all()
    return manager


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
    source: ToolSource | None = None,
) -> SessionHandle:
    ws = RootedWorkspace(settings.ws_root)
    roots = skill_roots(settings.ws_root)
    src = (
        source
        if source is not None
        else StaticToolSource((*builtin_tools(), SkillTool(roots)))
    )
    policy = StandardPolicy(
        model=settings.model,
        context_tokens=context_tokens,
        # Supplier, not snapshot: every prompt build sees the current toolset.
        tools=lambda: tuple(t.spec for t in src.all()),
        ws_root=ws.root,
        # Orientation order: who-you-are (identity, prepended by the policy),
        # then environment, project instructions, the repo map, the skills
        # catalog, and finally memory — broad context first, then the
        # session-specific learnings, then tools (appended by the policy).
        sections=(
            environment_section(lambda: _environment_facts(ws.root)),
            agents_doc_section(agents_doc_supplier(ws.root)),
            repo_map_section(repo_map_supplier(ws.root)),
            skills_section(skills_index_supplier(roots)),
            memory_section(memory_supplier(ws.root)),
        ),
        max_turns=settings.max_turns,
    )
    sid = sid if sid is not None else uuid.uuid4().hex
    store = JsonlStore(settings.sessions_root, sid)
    executor = ToolExecutor(src, ws)
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
    parser.add_argument(
        "--mcp-server",
        dest="mcp_server",
        action="append",
        metavar="SPEC",
        default=None,
        help="add one MCP server: 'name=command [args...]'; repeatable",
    )
    parser.add_argument(
        "--no-mcp",
        dest="no_mcp",
        action="store_true",
        help="skip mcp.toml loading (--mcp-server flags still apply)",
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
    manager = await connect_mcp(settings)
    try:
        handle = build_session(
            settings,
            provider=provider,
            asker=oneshot.StaticAsker(allow=False),
            context_tokens=info.context_tokens,
            source=build_tool_source(manager, skill_roots(settings.ws_root)),
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
    finally:
        # Teardown chains AFTER the session closed (run_once closes the
        # handle): no MCP child process outlives the run.
        if manager is not None:
            await manager.aclose()


async def _repl_main(settings: Settings) -> int:
    info = await build_provider(settings).info(settings.model)
    asker = repl.ConsoleAsker()
    roots = skill_roots(settings.ws_root)
    # The manager (and the one source over builtins + its tools) outlives
    # /clear: a fresh session reuses the same live toolset.
    manager = await connect_mcp(settings)
    source = build_tool_source(manager, roots)

    async def make_session() -> SessionHandle:
        # A fresh provider per session: /clear gets a clean fake script too.
        return build_session(
            settings,
            provider=build_provider(settings),
            asker=asker,
            context_tokens=info.context_tokens,
            source=source,
        )

    try:
        return await repl.run_repl(
            make_session,
            model=settings.model,
            pricing=info.pricing,
            mcp=manager,
            # /skills lists the catalog; /<name> runs a skill body; /remember
            # writes <ws_root>/.agent-forge/memory.md. discover_skills/
            # resolve_skill are stat-cached, so re-rendering each /skills is cheap.
            cwd=settings.ws_root,
            skills=lambda: render_skill_catalog(roots),
            skill_resolver=lambda name: resolve_skill(roots, name),
        )
    finally:
        if manager is not None:
            await manager.aclose()


def render_skill_catalog(roots: Sequence[Path]) -> str:
    """The /skills payload: the live skills catalog as '<name> — <desc>' lines.

    A zero-arg renderer (bound to roots) re-discovers on each call so a skill
    added mid-session shows up; discover_skills is stat-cached so it stays cheap.
    Empty -> the commands layer renders its own 'no skills found' line."""
    from forge.adapters.skills import render_catalog

    return render_catalog(discover_skills(roots))


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
