"""
mcp.py — Model Context Protocol integration.

Adapts MCP servers as tool sources. An ``MCPManager`` owns N ``MCPClient``
instances (one per declared server), each wrapping an ``MCPSession``
(Protocol) that talks to a spawned subprocess via stdio. MCP tools are
presented to the agent loop as plain ``Tool`` instances via the
``MCPTool`` adapter, namespaced ``{server}__{tool}`` to prevent collisions
when two servers ship a tool of the same name.

Optional dependency: the real stdio transport requires ``mcp>=1.0``
(install with ``pip install -e .[mcp]``). The module imports the SDK
lazily so ``import agent_forge.mcp`` always succeeds — the import only
runs when ``MCPClient.connect()`` is called with the default factory.
Tests substitute a fake ``MCPSession`` directly through the
``session_factory`` constructor kwarg, so the test suite does **not**
require the ``mcp`` SDK to be installed.

Lifecycle:
    cfg  = [MCPServerConfig(name="fs", command="mcp-fs", args=["/work"])]
    mgr  = MCPManager(cfg)
    await mgr.connect_all()              # fan-out connect (concurrent)
    registry.replace_mcp_tools(mgr.tools())
    ...
    await mgr.aclose()                   # fan-out close (concurrent)

Owns: ``MCPServerConfig``, ``MCPSession`` Protocol, ``MCPToolDescriptor``,
      ``MCPTool``, ``MCPClient``, ``MCPManager``, ``MCPServerStatus``,
      ``namespaced_tool_name``.
Does NOT own: any AgentRuntime / REPL composition (that lives in
              Phase H's ``build_runtime_with_mcp`` factory).
"""
from __future__ import annotations

import asyncio
import logging
import os
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from .messages import ToolDefinition, ToolResult

log = logging.getLogger(__name__)

# ── Server config ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MCPServerConfig:
    """Declarative config for one MCP server (stdio transport).

    Attributes:
        name:    short identifier used as the tool-name prefix
                 (``{name}__{tool}``). Must be unique within an
                 ``MCPManager``; should match ``[a-zA-Z0-9_-]+``.
        command: executable to spawn (resolved via ``PATH``).
        args:    arguments passed to the executable.
        env:     environment variables overlaid on the parent process env.
        enabled: when ``False``, ``MCPManager`` skips this server during
                 ``connect_all()`` — handy for toggling without editing
                 the config tuple.

    Future: an ``http`` transport variant will add ``url`` + ``headers``;
    keeping config frozen lets us evolve via subclasses without touching
    the existing callers.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


# ── Status ───────────────────────────────────────────────────────────────────

class MCPServerStatus(Enum):
    """Connection status for a single MCP server. Used by ``/mcp`` in Phase H."""
    DISCONNECTED = "disconnected"   # never attempted
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    CLOSED = "closed"


# ── Wire protocol — the small surface we need from any MCP client ────────────

@dataclass(frozen=True)
class MCPToolDescriptor:
    """The minimum we need to know about an MCP tool to wrap it as a Tool.

    Mirrors the ``tools/list`` response of the MCP spec. Kept as our own
    dataclass (not the SDK's type) so tests don't have to depend on the
    ``mcp`` package and so an SDK API change doesn't ripple through us.
    """
    name: str
    description: str
    input_schema: dict


class MCPSession(Protocol):
    """Structural type for any MCP client session.

    The real implementation wraps ``mcp.ClientSession`` (stdio transport)
    via the SDK. Tests supply an in-process fake.

    All three methods are awaitable. ``call_tool`` may raise (timeout,
    connection error) — ``MCPTool.execute`` catches and converts to
    ``ToolResult(is_error=True)``.
    """

    async def list_tools(self) -> list[MCPToolDescriptor]: ...
    async def call_tool(self, name: str, arguments: dict) -> str: ...
    async def aclose(self) -> None: ...


SessionFactory = Callable[[MCPServerConfig], Awaitable[MCPSession]]


# ── Tool naming ──────────────────────────────────────────────────────────────

_NAMESPACE_SEP = "__"


def namespaced_tool_name(server: str, tool: str) -> str:
    """Return ``{server}__{tool}`` — the name as the LLM sees it.

    The separator is ``__`` (two underscores) to stay clear of typical
    single-underscore identifiers and to round-trip cleanly through
    ``unpack_namespaced_name``.
    """
    return f"{server}{_NAMESPACE_SEP}{tool}"


def unpack_namespaced_name(qualified: str) -> tuple[str, str] | None:
    """Inverse of ``namespaced_tool_name``. Returns ``None`` if not namespaced.

    Used by ``MCPClient.call`` to route a qualified tool name back to
    the right server.
    """
    if _NAMESPACE_SEP not in qualified:
        return None
    server, _, tool = qualified.partition(_NAMESPACE_SEP)
    if not server or not tool:
        return None
    return server, tool


# ── MCPTool — adapter from MCP descriptor → Tool Protocol ────────────────────

class MCPTool:
    """Wraps one MCP tool so the agent loop sees a regular ``Tool``.

    The wrapped name is namespaced (``{server}__{tool}``); the original
    MCP-side name is preserved as ``mcp_name`` for the call-out, so the
    server still receives its native name.

    ``execute()`` ignores ``cwd`` and ``signal`` — MCP tools run inside the
    server's own process, not in our sandbox. Errors (server-side raise,
    transport error, timeout) are caught and returned as
    ``ToolResult(is_error=True)`` to honour the Tool Protocol contract.
    """

    def __init__(
        self,
        server: str,
        descriptor: MCPToolDescriptor,
        call: Callable[[str, dict], Awaitable[str]],
        *,
        timeout: float = 60.0,
    ) -> None:
        self._server = server
        self._descriptor = descriptor
        self._call = call
        self._timeout = timeout
        self._namespaced = namespaced_tool_name(server, descriptor.name)

    @property
    def name(self) -> str:
        return self._namespaced

    @property
    def description(self) -> str:
        # Prefix with [server] so the LLM has an at-a-glance source.
        return f"[{self._server}] {self._descriptor.description}"

    @property
    def parameters(self) -> dict:
        # MCP tools always have an object schema; pass through verbatim.
        # Empty/missing schema becomes a permissive object.
        return self._descriptor.input_schema or {"type": "object", "properties": {}}

    @property
    def mcp_name(self) -> str:
        """The tool's native name on the MCP server (no namespace prefix)."""
        return self._descriptor.name

    @property
    def server(self) -> str:
        return self._server

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    async def execute(
        self,
        args: dict,
        *,
        cwd: str,                                 # noqa: ARG002 — sandbox is server-side
        signal: asyncio.Event | None = None,      # noqa: ARG002 — no shared abort with server
    ) -> ToolResult:
        try:
            result = await asyncio.wait_for(
                self._call(self._descriptor.name, args),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                content=f"MCP tool '{self._namespaced}' timed out after {self._timeout}s",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — Tool Protocol forbids raising
            return ToolResult(
                content=f"MCP tool '{self._namespaced}' failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        # MCP servers return structured content; coerce to str if needed.
        if not isinstance(result, str):
            try:
                import json
                result = json.dumps(result, default=str)
            except Exception:  # noqa: BLE001 — last-ditch coercion
                result = str(result)
        return ToolResult(content=result)


# ── MCPClient — one server ───────────────────────────────────────────────────

class MCPClient:
    """Owns the lifecycle of one MCP server connection.

    Workflow::

        client = MCPClient(config)
        await client.connect()       # spawns subprocess, runs MCP handshake
        tools  = client.tools()      # list[MCPTool]
        ...
        await client.aclose()        # kills subprocess, drains session

    ``connect()`` is **not** idempotent — call it once per client. ``aclose()``
    is idempotent and never raises (mirrors AgentRuntime.aclose policy).

    Tests substitute the transport via ``session_factory``; the default
    factory uses the real ``mcp`` SDK over stdio. The factory is async
    because real MCP handshake involves multiple round-trips.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        session_factory: SessionFactory | None = None,
        call_timeout: float = 60.0,
    ) -> None:
        self.config = config
        self._session: MCPSession | None = None
        self._tools: list[MCPTool] = []
        self._status: MCPServerStatus = MCPServerStatus.DISCONNECTED
        self._error: str | None = None
        self._factory = session_factory or _default_stdio_session_factory
        self._call_timeout = call_timeout

    # ── Status read-only views ─────────────────────────────────────────

    @property
    def status(self) -> MCPServerStatus:
        return self._status

    @property
    def error(self) -> str | None:
        return self._error

    def tools(self) -> list[MCPTool]:
        return list(self._tools)

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Spawn the server and fetch its tool list. Sets status on outcome.

        On failure: status = FAILED, ``error`` carries a short message,
        ``tools()`` returns ``[]``. Does NOT raise — callers (MCPManager)
        decide whether one server's failure should be fatal or skipped.
        """
        if not self.config.enabled:
            self._status = MCPServerStatus.DISCONNECTED
            self._error = "disabled in config"
            return
        self._status = MCPServerStatus.CONNECTING
        try:
            self._session = await self._factory(self.config)
            descriptors = await self._session.list_tools()
        except Exception as exc:  # noqa: BLE001 — best-effort across N servers
            self._status = MCPServerStatus.FAILED
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("MCP server %r failed to connect: %s", self.config.name, self._error)
            # Tear down a half-built session so the subprocess doesn't leak.
            if self._session is not None:
                try:
                    await self._session.aclose()
                finally:
                    self._session = None
            return
        # Wire each descriptor to a closure that routes through this session.
        self._tools = [
            MCPTool(
                server=self.config.name,
                descriptor=d,
                call=self._call_tool,
                timeout=self._call_timeout,
            )
            for d in descriptors
        ]
        self._status = MCPServerStatus.CONNECTED
        self._error = None

    async def _call_tool(self, name: str, arguments: dict) -> str:
        """Internal: route a tool call through the live session.

        Raises if not connected — MCPTool.execute catches and converts.
        """
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not connected")
        return await self._session.call_tool(name, arguments)

    async def aclose(self) -> None:
        """Idempotent. Closes the session and forgets the tools."""
        if self._status == MCPServerStatus.CLOSED:
            return
        prev_status = self._status
        self._status = MCPServerStatus.CLOSED
        self._tools = []
        if self._session is not None:
            try:
                await self._session.aclose()
            except Exception as exc:  # noqa: BLE001 — never raise from cleanup
                log.warning(
                    "MCP server %r aclose error (was %s): %s",
                    self.config.name, prev_status.value, exc,
                )
            finally:
                self._session = None


# ── MCPManager — many servers ────────────────────────────────────────────────

class MCPManager:
    """Coordinator for N MCP servers. The thing AgentRuntime owns.

    Methods fan out concurrently across all clients so one slow server
    can't serialise the others. Failures are isolated: one server crashing
    on ``connect()`` does not stop the rest, and the resulting tool list
    simply skips the failed server.

    Typical use (Phase H factory will encapsulate this)::

        mgr = MCPManager(configs)
        await mgr.connect_all()
        registry.replace_mcp_tools(mgr.tools())
        # ... use ...
        await mgr.aclose()

    AgentRuntime calls ``aclose()`` via its lifecycle chain so the
    composition root doesn't have to remember.
    """

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        session_factory: SessionFactory | None = None,
        call_timeout: float = 60.0,
    ) -> None:
        # Reject duplicate server names — would collide in the tool namespace.
        seen: set[str] = set()
        for c in configs:
            if c.name in seen:
                raise ValueError(f"duplicate MCP server name: {c.name!r}")
            seen.add(c.name)
        self._clients: list[MCPClient] = [
            MCPClient(c, session_factory=session_factory, call_timeout=call_timeout)
            for c in configs
        ]
        self._connected = False

    # ── Read-only views ────────────────────────────────────────────────

    @property
    def clients(self) -> list[MCPClient]:
        return list(self._clients)

    def status(self) -> dict[str, MCPServerStatus]:
        """Per-server status map. Used by the ``/mcp`` slash command."""
        return {c.config.name: c.status for c in self._clients}

    def tools(self) -> list[MCPTool]:
        """Flat list of all tools from all CONNECTED clients."""
        return [t for c in self._clients for t in c.tools()]

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect_all(self) -> None:
        """Connect every client concurrently. Failures don't propagate."""
        if not self._clients:
            self._connected = True
            return
        # gather with return_exceptions=False would be fine — MCPClient.connect
        # already catches everything — but use return_exceptions=True for
        # belt-and-braces.
        await asyncio.gather(
            *(c.connect() for c in self._clients),
            return_exceptions=True,
        )
        self._connected = True

    async def reconnect(self, server_name: str) -> bool:
        """Close + connect a single named server. Returns True iff connected.

        Used by the ``/mcp reconnect <name>`` slash command (Phase H).
        """
        target = next((c for c in self._clients if c.config.name == server_name), None)
        if target is None:
            return False
        await target.aclose()
        # Reset client state so connect() works again. Simplest: rebuild it.
        idx = self._clients.index(target)
        self._clients[idx] = MCPClient(
            target.config,
            session_factory=target._factory,
            call_timeout=target._call_timeout,
        )
        await self._clients[idx].connect()
        return self._clients[idx].status == MCPServerStatus.CONNECTED

    async def aclose(self) -> None:
        """Concurrent fan-out shutdown. Idempotent. Never raises."""
        if not self._clients:
            return
        await asyncio.gather(
            *(c.aclose() for c in self._clients),
            return_exceptions=True,
        )


# ── Default session factory (real stdio MCP, lazy-imported) ──────────────────

async def _default_stdio_session_factory(config: MCPServerConfig) -> MCPSession:
    """Build a real stdio MCPSession using the ``mcp`` SDK.

    Lazy-imported so ``import agent_forge.mcp`` works without the optional
    dependency installed (e.g. when running the tests, which substitute
    their own ``session_factory``).

    Raises ``ImportError`` with an actionable install hint if ``mcp`` is
    not installed and the user actually tries to connect.
    """
    try:
        # SDK surface area we depend on:
        #   - mcp.ClientSession
        #   - mcp.StdioServerParameters
        #   - mcp.client.stdio.stdio_client (async context manager → (read, write))
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover — tested via env, not unit
        raise ImportError(
            "MCP support requires the optional 'mcp' dependency. "
            "Install with:  pip install agent-forge[mcp]"
        ) from exc

    params = StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env={**os.environ, **config.env},
    )

    # stdio_client is an async context manager; we keep it open for the
    # lifetime of the session and close it inside _StdioSessionAdapter.aclose.
    # We can't just `async with` here and return — the session would close on
    # exit. Instead, enter the CM manually and stash the exit coroutine for
    # later. Same pattern for the inner ClientSession.
    transport_cm = stdio_client(params)
    read, write = await transport_cm.__aenter__()
    session_cm = ClientSession(read, write)
    session = await session_cm.__aenter__()
    await session.initialize()
    return _StdioSessionAdapter(session, session_cm, transport_cm)


class _StdioSessionAdapter:
    """Adapts the real ``mcp.ClientSession`` to our ``MCPSession`` Protocol.

    Lives outside the factory so we have a clean place to handle the
    paired aenter/aexit of the two CMs (transport + session). aclose is
    best-effort — exceptions are swallowed and logged.
    """

    def __init__(self, session, session_cm, transport_cm) -> None:
        self._session = session
        self._session_cm = session_cm
        self._transport_cm = transport_cm

    async def list_tools(self) -> list[MCPToolDescriptor]:
        resp = await self._session.list_tools()
        # mcp 1.x returns an object with `.tools: list[mcp.Tool]` where each
        # mcp.Tool has .name, .description, .inputSchema. Be defensive:
        # accept either ducktyped attrs or a dict response.
        raw_tools = getattr(resp, "tools", None)
        if raw_tools is None and isinstance(resp, dict):
            raw_tools = resp.get("tools", [])
        return [
            MCPToolDescriptor(
                name=getattr(t, "name", None) or t["name"],
                description=getattr(t, "description", None) or t.get("description", ""),
                input_schema=(
                    getattr(t, "inputSchema", None)
                    or (t.get("inputSchema") if isinstance(t, dict) else None)
                    or {}
                ),
            )
            for t in (raw_tools or [])
        ]

    async def call_tool(self, name: str, arguments: dict) -> str:
        resp = await self._session.call_tool(name, arguments)
        # MCP CallToolResult: .content is a list of content blocks; each
        # has .type and a payload field (.text, .data, etc). Stringify
        # text blocks; for unknown blocks fall back to repr.
        content = getattr(resp, "content", None)
        if content is None and isinstance(resp, dict):
            content = resp.get("content", [])
        if not content:
            return ""
        parts: list[str] = []
        for blk in content:
            text = getattr(blk, "text", None)
            if text is None and isinstance(blk, dict):
                text = blk.get("text")
            parts.append(text if text is not None else repr(blk))
        return "\n".join(parts)

    async def aclose(self) -> None:
        # Reverse order: inner session first, then transport.
        for cm in (self._session_cm, self._transport_cm):
            try:
                await cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 — cleanup must not raise
                log.warning("MCP stdio adapter aclose: %s", exc)


# ── Config-file loader ───────────────────────────────────────────────────────

# TOML schema (loaded from ~/.agent-forge/mcp.toml and/or
# <cwd>/.agent-forge/mcp.toml; project overrides global on name collision):
#
#     [servers.fs]
#     command = "mcp-server-filesystem"
#     args    = ["/home/user/projects"]
#     enabled = true            # optional, defaults true
#
#     [servers.github]
#     command = "mcp-server-github"
#     env     = { GITHUB_TOKEN = "..." }
#
# Errors are non-fatal: a malformed file logs a warning and returns []
# for that location. Same pattern as `~/.agent-forge/plugins.toml`.

_GLOBAL_MCP_TOML = Path.home() / ".agent-forge" / "mcp.toml"


def _project_mcp_toml(cwd: str | os.PathLike[str]) -> Path:
    return Path(cwd) / ".agent-forge" / "mcp.toml"


def _parse_mcp_toml(path: Path) -> list[MCPServerConfig]:
    """Parse one mcp.toml. Returns [] if the file is missing or malformed."""
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
            log.warning("MCP config %s: server %r args must be list[str] — skipped", path, name)
            continue
        env = raw.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            log.warning("MCP config %s: server %r env must be dict[str,str] — skipped", path, name)
            continue
        enabled = bool(raw.get("enabled", True))
        out.append(MCPServerConfig(
            name=name, command=command,
            args=tuple(args), env=dict(env), enabled=enabled,
        ))
    return out


def load_mcp_configs(cwd: str | os.PathLike[str]) -> list[MCPServerConfig]:
    """Load MCP server configs from global + project mcp.toml files.

    Precedence: project (``<cwd>/.agent-forge/mcp.toml``) overrides global
    (``~/.agent-forge/mcp.toml``) on name collision. Both files are
    optional; either or both may be absent. Returns an empty list if no
    configs are found anywhere.

    Malformed files are skipped with a warning — they do not crash the
    REPL on startup.
    """
    by_name: dict[str, MCPServerConfig] = {}
    for cfg in _parse_mcp_toml(_GLOBAL_MCP_TOML):
        by_name[cfg.name] = cfg
    for cfg in _parse_mcp_toml(_project_mcp_toml(cwd)):
        by_name[cfg.name] = cfg
    return list(by_name.values())


def parse_mcp_server_spec(spec: str) -> MCPServerConfig:
    """Parse a ``--mcp-server`` CLI value.

    Format: ``name=command [arg1 arg2 ...]``. The leftmost ``=`` splits
    name from the command-line; the command-line is then shell-tokenised
    (``shlex.split``) so quoted args survive.

    Raises ``ValueError`` on a malformed spec — caller surfaces the
    message via argparse.
    """
    import shlex
    if "=" not in spec:
        raise ValueError(
            f"--mcp-server: expected 'name=command [args...]', got {spec!r}"
        )
    name, _, cmdline = spec.partition("=")
    name = name.strip()
    cmdline = cmdline.strip()
    if not name or not cmdline:
        raise ValueError(f"--mcp-server: empty name or command in {spec!r}")
    tokens = shlex.split(cmdline)
    if not tokens:
        raise ValueError(f"--mcp-server: no command tokens in {spec!r}")
    return MCPServerConfig(name=name, command=tokens[0], args=tuple(tokens[1:]))
