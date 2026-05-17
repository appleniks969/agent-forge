"""In-process fake MCP session for tests.

agent_forge.mcp.MCPSession is a Protocol — anything async with the three
required methods works. The real implementation talks to a spawned
subprocess over stdio via the `mcp` SDK; this fake substitutes that with
in-memory data and configurable failure modes so the test suite never
needs the SDK installed or a subprocess running.

Usage::

    from tests.fake_mcp_server import FakeMCPSession, fake_session_factory

    session = FakeMCPSession(tools=[
        ("echo", "Echoes input", {"type": "object", ...}, lambda args: args["text"]),
    ])

    # Or wire as a factory for MCPClient / MCPManager:
    cfg = MCPServerConfig(name="fake", command="x")
    client = MCPClient(cfg, session_factory=fake_session_factory({"fake": session}))

Failure modes:
    raise_on_list   - list_tools raises this exception
    raise_on_call   - call_tool raises this exception (for any name)
    slow_call_sec   - call_tool sleeps this long before returning

aclose() increments .closed_count so tests can assert lifecycle is honoured.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from agent_forge.mcp import MCPServerConfig, MCPSession, MCPToolDescriptor


ToolHandler = Callable[[dict], str]
ToolSpec = tuple[str, str, dict, ToolHandler]   # (name, description, schema, handler)


class FakeMCPSession:
    """An MCPSession Protocol implementation backed by in-memory handlers."""

    def __init__(
        self,
        tools: list[ToolSpec] | None = None,
        *,
        raise_on_list: Exception | None = None,
        raise_on_call: Exception | None = None,
        slow_call_sec: float = 0.0,
    ) -> None:
        self._tools = tools or []
        self.raise_on_list = raise_on_list
        self.raise_on_call = raise_on_call
        self.slow_call_sec = slow_call_sec
        # Telemetry the tests assert on
        self.list_calls: int = 0
        self.call_calls: list[tuple[str, dict]] = []
        self.closed_count: int = 0

    async def list_tools(self) -> list[MCPToolDescriptor]:
        self.list_calls += 1
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return [
            MCPToolDescriptor(name=n, description=d, input_schema=s)
            for (n, d, s, _) in self._tools
        ]

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.call_calls.append((name, arguments))
        if self.slow_call_sec > 0:
            await asyncio.sleep(self.slow_call_sec)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        for n, _d, _s, handler in self._tools:
            if n == name:
                return handler(arguments)
        raise KeyError(f"fake server has no tool named {name!r}")

    async def aclose(self) -> None:
        self.closed_count += 1


def fake_session_factory(
    sessions_by_server: dict[str, FakeMCPSession],
) -> Callable[[MCPServerConfig], Awaitable[MCPSession]]:
    """Return a SessionFactory that hands out pre-built fakes by server name.

    Raises ``KeyError`` if a config references an unknown server — this
    propagates to ``MCPClient.connect()`` and is recorded as a connect
    failure on that client (good — exercises the error path).
    """

    async def _factory(config: MCPServerConfig) -> MCPSession:
        if config.name not in sessions_by_server:
            raise KeyError(f"no fake registered for server {config.name!r}")
        return sessions_by_server[config.name]

    return _factory


def failing_factory(exc: Exception) -> Callable[[MCPServerConfig], Awaitable[MCPSession]]:
    """A factory that always raises — to test connect-failure isolation."""

    async def _factory(config: MCPServerConfig) -> MCPSession:  # noqa: ARG001
        raise exc

    return _factory
