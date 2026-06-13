"""MCP adapter: connects to MCP servers and manufactures Tools from them.

Layer: adapters — imports ports + kernel only; asyncio is allowed here.
MCPManager owns connect AND teardown for many servers; a broken server is
reported via status(), never raised at startup, and reconnect(name) restores
one server without touching the rest. Each server tool surfaces as a Tool
named "{server}__{tool}" whose Effects come from MCP annotations; unannotated
tools default to WRITE_PATH|EXEC|EXTERNAL (EXTERNAL => the guard chain
defaults to Ask) because we do not trust self-description with parallelism
or auto-allow. Child processes get a scrubbed env (allowlist + declared
vars), never the full host env. The mcp SDK import is lazy inside connect();
tests inject fakes through the session_factory seam. Config parsing
(mcp.toml + --mcp-server specs) lives here next to MCPServerConfig;
deciding WHICH configs apply stays in front/wiring.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from forge.kernel.types import Effects, ToolResult, ToolSpec
from forge.ports.source import ToolSource
from forge.ports.tool import ToolCtx

log = logging.getLogger(__name__)


# --- config and status ------------------------------------------------------


@dataclass(frozen=True)
class MCPServerConfig:
    """One declared stdio server; name is the tool-namespace prefix."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True


class ServerStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    CLOSED = "closed"


# --- wire surface: the minimum we need from any MCP client -------------------


@dataclass(frozen=True)
class MCPToolDescriptor:
    """Our own view of a tools/list entry; None hints mean unannotated."""

    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    read_only: bool | None = None  # MCP annotations.readOnlyHint
    destructive: bool | None = None  # MCP annotations.destructiveHint


class MCPSession(Protocol):
    async def list_tools(self) -> Sequence[MCPToolDescriptor]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str: ...

    async def aclose(self) -> None: ...


SessionFactory = Callable[[MCPServerConfig], Awaitable[MCPSession]]


# --- env scrubbing -----------------------------------------------------------

ENV_ALLOWLIST: frozenset[str] = frozenset({"PATH", "HOME", "LANG"})


def scrub_env(host: Mapping[str, str], declared: Mapping[str, str]) -> dict[str, str]:
    """Child env = allowlisted host vars + config-declared vars, nothing else."""
    env = {k: host[k] for k in ENV_ALLOWLIST if k in host}
    env.update(declared)
    return env


# --- annotations -> Effects ---------------------------------------------------

UNANNOTATED_EFFECTS = Effects.WRITE_PATH | Effects.EXEC | Effects.EXTERNAL


def effects_from_annotations(read_only: bool | None, destructive: bool | None) -> Effects:
    """Map MCP hints to Effects; absent or alarming hints get the full-caution set."""
    if read_only:
        return Effects.READ_PATH
    if destructive is False:
        # Explicitly non-destructive write; still serialized, no Ask default.
        return Effects.WRITE_PATH
    # Unannotated, or not-read-only with destructive unset/true (the MCP
    # spec defaults destructiveHint to true): EXTERNAL keeps Ask in play.
    return UNANNOTATED_EFFECTS


# --- tool naming --------------------------------------------------------------

_SEP = "__"


def namespaced_name(server: str, tool: str) -> str:
    return f"{server}{_SEP}{tool}"


# --- MCPTool: one server tool behind the Tool port ----------------------------


def _coerce_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


class MCPTool:
    """Wraps one MCP tool as a Tool; the server still sees its native name."""

    def __init__(
        self,
        server: str,
        descriptor: MCPToolDescriptor,
        call: Callable[[str, dict[str, Any]], Awaitable[str]],
        *,
        timeout: float = 60.0,
    ) -> None:
        self._remote_name = descriptor.name
        self._call = call
        self._timeout = timeout
        self.spec = ToolSpec(
            name=namespaced_name(server, descriptor.name),
            description=f"[{server}] {descriptor.description}",
            params=dict(descriptor.input_schema) or {"type": "object", "properties": {}},
            effects=effects_from_annotations(descriptor.read_only, descriptor.destructive),
        )

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        # call_id is stamped by the executor; ctx.ws is unused — MCP tools run
        # inside the server's own process, outside our Workspace authority.
        call = asyncio.ensure_future(self._call(self._remote_name, dict(args)))
        cancelled = asyncio.ensure_future(ctx.cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {call, cancelled},
                timeout=self._timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if call in done:
                exc = call.exception()
                if exc is not None:
                    return self._error(f"failed: {type(exc).__name__}: {exc}")
                return ToolResult(call_id="", content=_coerce_text(call.result()))
            if cancelled in done:
                return self._error("cancelled")
            return self._error(f"timed out after {self._timeout}s")
        finally:
            for task in (call, cancelled):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    def _error(self, detail: str) -> ToolResult:
        return ToolResult(
            call_id="", content=f"MCP tool '{self.spec.name}' {detail}", is_error=True
        )


# --- one server's lifecycle ----------------------------------------------------


class _Server:
    """connect/aclose for one config; every failure lands in status, never raises."""

    def __init__(self, config: MCPServerConfig, factory: SessionFactory, call_timeout: float) -> None:
        self.config = config
        self._factory = factory
        self._call_timeout = call_timeout
        self.status = ServerStatus.DISCONNECTED
        self.error: str | None = None
        self.tools: tuple[MCPTool, ...] = ()
        self._session: MCPSession | None = None

    async def connect(self) -> None:
        if not self.config.enabled:
            self.status = ServerStatus.DISCONNECTED
            self.error = "disabled in config"
            return
        self.status = ServerStatus.CONNECTING
        try:
            self._session = await self._factory(self.config)
            descriptors = await self._session.list_tools()
        except Exception as exc:  # noqa: BLE001 — broken servers report, never raise
            self.status = ServerStatus.FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("MCP server %r failed to connect: %s", self.config.name, self.error)
            await self._drop_session()
            return
        self.tools = tuple(
            MCPTool(self.config.name, d, self._call_tool, timeout=self._call_timeout)
            for d in descriptors
        )
        self.status = ServerStatus.CONNECTED
        self.error = None

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        # Raises when disconnected; MCPTool.run converts to an error ToolResult.
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not connected")
        return await self._session.call_tool(name, arguments)

    async def aclose(self) -> None:
        if self.status is ServerStatus.CLOSED:
            return
        self.status = ServerStatus.CLOSED
        self.tools = ()
        await self._drop_session()

    async def _drop_session(self) -> None:
        if self._session is None:
            return
        session, self._session = self._session, None
        try:
            await session.aclose()
        except Exception as exc:  # noqa: BLE001 — cleanup never raises
            log.warning("MCP server %r close error: %s", self.config.name, exc)


# --- MCPManager: many servers, one owner ----------------------------------------


class MCPManager:
    """Owns connect AND teardown for every configured server."""

    def __init__(
        self,
        configs: Sequence[MCPServerConfig],
        *,
        session_factory: SessionFactory | None = None,
        call_timeout: float = 60.0,
    ) -> None:
        names = [c.name for c in configs]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate MCP server name(s): {duplicates}")
        factory = session_factory or _stdio_session_factory
        self._servers = {c.name: _Server(c, factory, call_timeout) for c in configs}
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic change token: bumps whenever the toolset may have changed."""
        return self._generation

    async def connect_all(self) -> None:
        """Connect every enabled server concurrently; failures stay per-server."""
        await asyncio.gather(
            *(s.connect() for s in self._servers.values()), return_exceptions=True
        )
        self._generation += 1

    async def reconnect(self, name: str) -> bool:
        """Close + connect one server. True iff it ends up connected."""
        server = self._servers.get(name)
        if server is None:
            return False
        await server.aclose()
        await server.connect()
        self._generation += 1
        return server.status is ServerStatus.CONNECTED

    async def aclose(self) -> None:
        """Concurrent fan-out shutdown; idempotent; never raises."""
        await asyncio.gather(
            *(s.aclose() for s in self._servers.values()), return_exceptions=True
        )
        self._generation += 1

    def status(self) -> dict[str, ServerStatus]:
        return {name: s.status for name, s in self._servers.items()}

    def errors(self) -> dict[str, str]:
        return {name: s.error for name, s in self._servers.items() if s.error is not None}

    def tools(self) -> tuple[MCPTool, ...]:
        return tuple(t for s in self._servers.values() for t in s.tools)

    def tool_counts(self) -> dict[str, int]:
        return {name: len(s.tools) for name, s in self._servers.items()}

    def source(self) -> ToolSource:
        """A live ToolSource over this manager: lookups see the CURRENT toolset,
        so a reconnect's tool swap reaches consumers without re-wiring."""
        return _ManagerToolSource(self)


class _ManagerToolSource:
    """Name-indexed view; the index rebuilds whenever the generation moves."""

    def __init__(self, manager: MCPManager) -> None:
        self._manager = manager
        self._indexed_at = -1
        self._by_name: dict[str, MCPTool] = {}

    @property
    def generation(self) -> int:
        return self._manager.generation

    def get(self, name: str) -> MCPTool | None:
        return self._index().get(name)

    def all(self) -> tuple[MCPTool, ...]:
        return tuple(self._index().values())

    def _index(self) -> dict[str, MCPTool]:
        generation = self._manager.generation
        if generation != self._indexed_at:
            self._by_name = {t.spec.name: t for t in self._manager.tools()}
            self._indexed_at = generation
        return self._by_name


# --- default factory: real stdio transport, SDK imported lazily ------------------


async def _stdio_session_factory(config: MCPServerConfig) -> MCPSession:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError(
            "MCP support requires the optional 'mcp' dependency; "
            "install with: pip install forge-v2[mcp]"
        ) from exc

    params = StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=scrub_env(os.environ, config.env),
    )
    # The SDK's context managers hide anyio cancel scopes that must be entered
    # and exited by the SAME task, but connect_all()/aclose() run in different
    # gather children. A dedicated lifecycle task owns both CMs end-to-end;
    # connect hands the session out via a future, close just sets an event.
    ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    close = asyncio.Event()

    async def lifecycle() -> None:
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    ready.set_result(session)
                    await close.wait()
        except BaseException as exc:  # noqa: BLE001 — surfaces via the future
            if not ready.done():
                ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                log.warning("MCP server %r stdio teardown: %s", config.name, exc)

    task = asyncio.create_task(lifecycle(), name=f"mcp-stdio:{config.name}")
    try:
        session = await ready
    except BaseException:
        close.set()
        with contextlib.suppress(BaseException):
            await task
        raise
    return _StdioSession(session, close, task)


class _StdioSession:
    """Adapts mcp.ClientSession to MCPSession; SDK types do not leak past here."""

    _CLOSE_TIMEOUT = 5.0

    def __init__(self, session: Any, close: asyncio.Event, task: asyncio.Task[None]) -> None:
        self._session = session
        self._close = close
        self._task = task

    async def list_tools(self) -> list[MCPToolDescriptor]:
        resp = await self._session.list_tools()
        out: list[MCPToolDescriptor] = []
        for t in getattr(resp, "tools", None) or []:
            ann = getattr(t, "annotations", None)
            out.append(
                MCPToolDescriptor(
                    name=t.name,
                    description=getattr(t, "description", None) or "",
                    input_schema=getattr(t, "inputSchema", None) or {},
                    read_only=getattr(ann, "readOnlyHint", None) if ann else None,
                    destructive=getattr(ann, "destructiveHint", None) if ann else None,
                )
            )
        return out

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        resp = await self._session.call_tool(name, arguments)
        blocks = getattr(resp, "content", None) or []
        parts: list[str] = []
        for blk in blocks:
            text = getattr(blk, "text", None)
            parts.append(text if text is not None else repr(blk))
        return "\n".join(parts)

    async def aclose(self) -> None:
        # Unblocks the lifecycle task, which exits both SDK context managers
        # in the task that entered them; a hung child process gets cancelled.
        self._close.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), self._CLOSE_TIMEOUT)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        except Exception as exc:  # noqa: BLE001 — cleanup never raises
            log.warning("MCP stdio close: %s", exc)


# --- config loading: mcp.toml + --mcp-server specs (v1-compatible schema) ---------
#
#     [servers.fs]
#     command = "mcp-server-filesystem"
#     args    = ["/home/me/projects"]      # optional
#     env     = { GITHUB_TOKEN = "..." }   # optional
#     enabled = true                        # optional, defaults true
#
# Malformed files or entries are logged and skipped — startup never crashes
# over one bad server declaration.

_CONFIG_RELPATH = Path(".agent-forge") / "mcp.toml"


def _parse_mcp_toml(path: Path) -> list[MCPServerConfig]:
    """Parse one mcp.toml; missing or malformed files yield []."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    except (tomllib.TOMLDecodeError, OSError) as exc:
        log.warning("MCP config %s: parse failed (%s) — skipped", path, exc)
        return []

    servers = data.get("servers")
    if not isinstance(servers, dict):
        return []

    out: list[MCPServerConfig] = []
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            log.warning("MCP config %s: server %r is not a table — skipped", path, name)
            continue
        command = raw.get("command")
        if not isinstance(command, str) or not command:
            log.warning("MCP config %s: server %r missing command — skipped", path, name)
            continue
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            log.warning(
                "MCP config %s: server %r args must be list[str] — skipped", path, name
            )
            continue
        env = raw.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            log.warning(
                "MCP config %s: server %r env must be dict[str,str] — skipped", path, name
            )
            continue
        out.append(
            MCPServerConfig(
                name=name,
                command=command,
                args=tuple(args),
                env=dict(env),
                enabled=bool(raw.get("enabled", True)),
            )
        )
    return out


def load_mcp_configs(
    cwd: str | os.PathLike[str], *, home: Path | None = None
) -> list[MCPServerConfig]:
    """Load configs from ~/.agent-forge/mcp.toml then <cwd>/.agent-forge/mcp.toml;
    project entries override global by server name. Both files are optional."""
    home = home if home is not None else Path.home()
    by_name: dict[str, MCPServerConfig] = {}
    for cfg in _parse_mcp_toml(home / _CONFIG_RELPATH):
        by_name[cfg.name] = cfg
    for cfg in _parse_mcp_toml(Path(cwd) / _CONFIG_RELPATH):
        by_name[cfg.name] = cfg
    return list(by_name.values())


def parse_mcp_server_spec(spec: str) -> MCPServerConfig:
    """Parse one --mcp-server value: 'name=command [args...]' (args shell-tokenised).

    Raises ValueError on a malformed spec — the CLI surfaces the message.
    """
    if "=" not in spec:
        raise ValueError(f"--mcp-server: expected 'name=command [args...]', got {spec!r}")
    name, _, cmdline = spec.partition("=")
    name = name.strip()
    cmdline = cmdline.strip()
    if not name or not cmdline:
        raise ValueError(f"--mcp-server: empty name or command in {spec!r}")
    tokens = shlex.split(cmdline)
    if not tokens:
        raise ValueError(f"--mcp-server: no command tokens in {spec!r}")
    return MCPServerConfig(name=name, command=tokens[0], args=tuple(tokens[1:]))
