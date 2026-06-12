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
tests inject fakes through the session_factory seam. TOML/CLI config parsing
lives in front/wiring.py, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from forge.kernel.types import Effects, ToolResult, ToolSpec
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

    async def connect_all(self) -> None:
        """Connect every enabled server concurrently; failures stay per-server."""
        await asyncio.gather(
            *(s.connect() for s in self._servers.values()), return_exceptions=True
        )

    async def reconnect(self, name: str) -> bool:
        """Close + connect one server. True iff it ends up connected."""
        server = self._servers.get(name)
        if server is None:
            return False
        await server.aclose()
        await server.connect()
        return server.status is ServerStatus.CONNECTED

    async def aclose(self) -> None:
        """Concurrent fan-out shutdown; idempotent; never raises."""
        await asyncio.gather(
            *(s.aclose() for s in self._servers.values()), return_exceptions=True
        )

    def status(self) -> dict[str, ServerStatus]:
        return {name: s.status for name, s in self._servers.items()}

    def errors(self) -> dict[str, str]:
        return {name: s.error for name, s in self._servers.items() if s.error is not None}

    def tools(self) -> tuple[MCPTool, ...]:
        return tuple(t for s in self._servers.values() for t in s.tools)


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
    # Enter both context managers by hand and pair the exits in aclose() —
    # `async with` here would tear the session down on return.
    transport_cm = stdio_client(params)
    read, write = await transport_cm.__aenter__()
    session_cm = ClientSession(read, write)
    session = await session_cm.__aenter__()
    await session.initialize()
    return _StdioSession(session, session_cm, transport_cm)


class _StdioSession:
    """Adapts mcp.ClientSession to MCPSession; SDK types do not leak past here."""

    def __init__(self, session: Any, session_cm: Any, transport_cm: Any) -> None:
        self._session = session
        self._session_cm = session_cm
        self._transport_cm = transport_cm

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
        # Reverse order of entry: session first, then transport.
        for cm in (self._session_cm, self._transport_cm):
            try:
                await cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 — cleanup never raises
                log.warning("MCP stdio close: %s", exc)
