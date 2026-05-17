"""Tests for MCP core: MCPTool / MCPClient / MCPManager + registry hot-swap."""
from __future__ import annotations

import asyncio

import pytest

from agent_forge.mcp import (
    MCPClient, MCPManager, MCPServerConfig, MCPServerStatus, MCPTool,
    MCPToolDescriptor, namespaced_tool_name, unpack_namespaced_name,
)
from agent_forge.runtime import AgentRuntime
from agent_forge.system_prompt import SystemPrompt
from agent_forge.models import MODELS
from agent_forge.tools import ToolRegistry, default_registry

from tests.fake_mcp_server import (
    FakeMCPSession, fake_session_factory, failing_factory,
)


MODEL = MODELS["claude-sonnet-4-6"]


# ── Namespace helpers ────────────────────────────────────────────────────────

def test_namespaced_tool_name_format():
    assert namespaced_tool_name("fs", "read") == "fs__read"


def test_unpack_namespaced_round_trips():
    assert unpack_namespaced_name("fs__read") == ("fs", "read")


def test_unpack_namespaced_returns_none_for_unnamespaced():
    assert unpack_namespaced_name("plain") is None


def test_unpack_namespaced_rejects_empty_halves():
    assert unpack_namespaced_name("__read") is None
    assert unpack_namespaced_name("fs__") is None


# ── MCPTool adapter ──────────────────────────────────────────────────────────

async def test_mcp_tool_namespaces_name_and_prefixes_description():
    calls = []

    async def call(name, args):
        calls.append((name, args))
        return "ok"

    desc = MCPToolDescriptor(
        name="read", description="Read a file",
        input_schema={"type": "object", "properties": {"p": {"type": "string"}}},
    )
    t = MCPTool(server="fs", descriptor=desc, call=call)
    assert t.name == "fs__read"
    assert t.description.startswith("[fs]")
    assert t.mcp_name == "read"
    assert t.server == "fs"
    # Definition forwards the schema
    d = t.definition()
    assert d.name == "fs__read"
    assert d.parameters["properties"]["p"]["type"] == "string"


async def test_mcp_tool_execute_routes_through_call_with_original_name():
    received = []

    async def call(name, args):
        received.append((name, args))
        return "hello"

    desc = MCPToolDescriptor(name="echo", description="", input_schema={})
    t = MCPTool(server="srv", descriptor=desc, call=call)
    result = await t.execute({"x": 1}, cwd=".")
    assert not result.is_error
    assert result.content == "hello"
    # Server gets the un-namespaced name, not "srv__echo"
    assert received == [("echo", {"x": 1})]


async def test_mcp_tool_execute_handles_timeout():
    async def call(name, args):
        await asyncio.sleep(1.0)
        return "never"

    desc = MCPToolDescriptor(name="slow", description="", input_schema={})
    t = MCPTool(server="s", descriptor=desc, call=call, timeout=0.05)
    result = await t.execute({}, cwd=".")
    assert result.is_error
    assert "timed out" in result.content


async def test_mcp_tool_execute_handles_call_exception():
    async def call(name, args):
        raise RuntimeError("server crashed")

    desc = MCPToolDescriptor(name="boom", description="", input_schema={})
    t = MCPTool(server="s", descriptor=desc, call=call)
    result = await t.execute({}, cwd=".")
    assert result.is_error
    assert "RuntimeError" in result.content
    assert "server crashed" in result.content


async def test_mcp_tool_execute_coerces_non_string_result():
    async def call(name, args):
        return {"k": "v"}  # MCP server might return structured data

    desc = MCPToolDescriptor(name="data", description="", input_schema={})
    t = MCPTool(server="s", descriptor=desc, call=call)
    result = await t.execute({}, cwd=".")
    assert not result.is_error
    assert '"k"' in result.content and '"v"' in result.content


# ── MCPClient lifecycle ──────────────────────────────────────────────────────

def _spec(name="echo", description="echo input", schema=None):
    return (
        name,
        description,
        schema or {"type": "object", "properties": {"text": {"type": "string"}}},
        lambda args: args.get("text", ""),
    )


async def test_client_connects_and_lists_tools():
    sess = FakeMCPSession(tools=[_spec(), _spec("upper", "uppercase")])
    cfg = MCPServerConfig(name="fake", command="x")
    client = MCPClient(cfg, session_factory=fake_session_factory({"fake": sess}))
    await client.connect()
    assert client.status == MCPServerStatus.CONNECTED
    assert client.error is None
    names = [t.name for t in client.tools()]
    assert names == ["fake__echo", "fake__upper"]
    assert sess.list_calls == 1


async def test_client_skips_when_disabled():
    sess = FakeMCPSession(tools=[_spec()])
    cfg = MCPServerConfig(name="fake", command="x", enabled=False)
    client = MCPClient(cfg, session_factory=fake_session_factory({"fake": sess}))
    await client.connect()
    assert client.status == MCPServerStatus.DISCONNECTED
    assert "disabled" in (client.error or "")
    assert client.tools() == []
    # Disabled = no factory call, so session never even instantiated/asked
    assert sess.list_calls == 0


async def test_client_records_failure_without_raising():
    cfg = MCPServerConfig(name="bad", command="x")
    client = MCPClient(cfg, session_factory=failing_factory(RuntimeError("no exe")))
    await client.connect()  # must not raise
    assert client.status == MCPServerStatus.FAILED
    assert "no exe" in (client.error or "")
    assert client.tools() == []


async def test_client_failure_during_list_tools_tears_down_session():
    """If list_tools raises after the session is created, the session must
    still be closed so we don't leak subprocesses."""
    sess = FakeMCPSession(raise_on_list=RuntimeError("list failed"))
    cfg = MCPServerConfig(name="fake", command="x")
    client = MCPClient(cfg, session_factory=fake_session_factory({"fake": sess}))
    await client.connect()
    assert client.status == MCPServerStatus.FAILED
    assert sess.closed_count == 1  # cleanup ran


async def test_client_aclose_is_idempotent():
    sess = FakeMCPSession(tools=[_spec()])
    cfg = MCPServerConfig(name="fake", command="x")
    client = MCPClient(cfg, session_factory=fake_session_factory({"fake": sess}))
    await client.connect()
    await client.aclose()
    await client.aclose()  # no-op second time
    assert sess.closed_count == 1
    assert client.status == MCPServerStatus.CLOSED
    assert client.tools() == []


async def test_client_tool_call_routes_through_live_session():
    sess = FakeMCPSession(tools=[_spec()])
    cfg = MCPServerConfig(name="fake", command="x")
    client = MCPClient(cfg, session_factory=fake_session_factory({"fake": sess}))
    await client.connect()
    tool = client.tools()[0]
    result = await tool.execute({"text": "hi"}, cwd=".")
    assert result.content == "hi"
    assert sess.call_calls == [("echo", {"text": "hi"})]


async def test_client_tool_call_after_close_returns_error():
    sess = FakeMCPSession(tools=[_spec()])
    cfg = MCPServerConfig(name="fake", command="x")
    client = MCPClient(cfg, session_factory=fake_session_factory({"fake": sess}))
    await client.connect()
    tool = client.tools()[0]
    await client.aclose()
    # The tool reference is still alive; invocation should error cleanly.
    # (After aclose, tools() returns [] but the LLM may hold a stale ref.)
    result = await tool.execute({"text": "hi"}, cwd=".")
    assert result.is_error
    assert "not connected" in result.content


# ── MCPManager fan-out ───────────────────────────────────────────────────────

async def test_manager_rejects_duplicate_server_names():
    with pytest.raises(ValueError, match="duplicate"):
        MCPManager([
            MCPServerConfig(name="fs", command="a"),
            MCPServerConfig(name="fs", command="b"),
        ])


async def test_manager_connects_all_servers_concurrently():
    sess_a = FakeMCPSession(tools=[_spec("a")])
    sess_b = FakeMCPSession(tools=[_spec("b")])
    sess_c = FakeMCPSession(tools=[_spec("c")])
    mgr = MCPManager(
        [
            MCPServerConfig(name="A", command="x"),
            MCPServerConfig(name="B", command="x"),
            MCPServerConfig(name="C", command="x"),
        ],
        session_factory=fake_session_factory({"A": sess_a, "B": sess_b, "C": sess_c}),
    )
    await mgr.connect_all()
    statuses = mgr.status()
    assert all(s == MCPServerStatus.CONNECTED for s in statuses.values())
    tool_names = [t.name for t in mgr.tools()]
    assert tool_names == ["A__a", "B__b", "C__c"]


async def test_manager_isolates_one_failed_server():
    """One server failing must not stop the others from being usable."""
    sess_ok = FakeMCPSession(tools=[_spec("ok")])
    mgr = MCPManager(
        [
            MCPServerConfig(name="bad", command="x"),
            MCPServerConfig(name="good", command="x"),
        ],
        session_factory=fake_session_factory({"good": sess_ok}),  # 'bad' missing
    )
    await mgr.connect_all()
    status = mgr.status()
    assert status["bad"] == MCPServerStatus.FAILED
    assert status["good"] == MCPServerStatus.CONNECTED
    # Tools list only includes the healthy server
    assert [t.name for t in mgr.tools()] == ["good__ok"]


async def test_manager_aclose_closes_every_client_even_if_one_failed():
    sess_ok = FakeMCPSession(tools=[_spec()])
    mgr = MCPManager(
        [
            MCPServerConfig(name="bad", command="x"),
            MCPServerConfig(name="good", command="x"),
        ],
        session_factory=fake_session_factory({"good": sess_ok}),
    )
    await mgr.connect_all()
    await mgr.aclose()
    # The good one was closed; the bad one never had a session but its
    # aclose still ran (no crash).
    assert sess_ok.closed_count == 1


async def test_manager_aclose_with_empty_clients_is_noop():
    mgr = MCPManager([])
    await mgr.aclose()
    await mgr.connect_all()  # also fine
    assert mgr.tools() == []
    assert mgr.status() == {}


async def test_manager_reconnect_resets_a_failed_server():
    """A failed server can be reconnected once the user fixes the cause."""
    # First time: fail
    fails = {"calls": 0}

    async def factory(cfg):
        fails["calls"] += 1
        if fails["calls"] == 1:
            raise RuntimeError("first attempt failed")
        return FakeMCPSession(tools=[_spec("ok")])

    mgr = MCPManager([MCPServerConfig(name="srv", command="x")], session_factory=factory)
    await mgr.connect_all()
    assert mgr.status()["srv"] == MCPServerStatus.FAILED

    ok = await mgr.reconnect("srv")
    assert ok is True
    assert mgr.status()["srv"] == MCPServerStatus.CONNECTED
    assert [t.name for t in mgr.tools()] == ["srv__ok"]


async def test_manager_reconnect_unknown_server_returns_false():
    mgr = MCPManager([MCPServerConfig(name="known", command="x")])
    assert await mgr.reconnect("missing") is False


# ── ToolRegistry hot-swap ────────────────────────────────────────────────────

class _DummyTool:
    """Minimal Tool stub for registry tests — no exec, just identity."""

    def __init__(self, name: str):
        self.name = name
        self.description = f"d-{name}"
        self.parameters = {"type": "object", "properties": {}}

    def definition(self):
        from agent_forge.messages import ToolDefinition
        return ToolDefinition(self.name, self.description, self.parameters)

    async def execute(self, args, *, cwd, signal=None):  # pragma: no cover
        from agent_forge.messages import ToolResult
        return ToolResult(content="")


def test_registry_replace_mcp_tools_first_call_adds():
    reg = ToolRegistry()
    reg.register(_DummyTool("Bash"))  # non-MCP built-in stand-in
    reg.replace_mcp_tools([_DummyTool("fs__read"), _DummyTool("fs__write")])
    assert set(reg.names()) == {"Bash", "fs__read", "fs__write"}
    assert set(reg.mcp_names()) == {"fs__read", "fs__write"}


def test_registry_replace_mcp_tools_swaps_atomically():
    """A second call must drop the first batch and add the second — never both."""
    reg = ToolRegistry()
    reg.register(_DummyTool("Bash"))
    reg.replace_mcp_tools([_DummyTool("fs__read")])
    assert "fs__read" in reg.names()

    reg.replace_mcp_tools([_DummyTool("gh__list"), _DummyTool("gh__create")])
    assert "fs__read" not in reg.names()
    assert set(reg.mcp_names()) == {"gh__list", "gh__create"}
    assert "Bash" in reg.names()  # built-in untouched


def test_registry_replace_with_empty_clears_mcp_only():
    reg = default_registry()
    builtin_count = len(reg.names())
    reg.replace_mcp_tools([_DummyTool("x__y")])
    assert len(reg.names()) == builtin_count + 1
    reg.replace_mcp_tools([])
    assert len(reg.names()) == builtin_count
    assert reg.mcp_names() == []


def test_register_after_replace_demotes_to_non_mcp():
    """If a user later registers a name that was MCP, it must lose MCP tag."""
    reg = ToolRegistry()
    reg.replace_mcp_tools([_DummyTool("fs__read")])
    assert "fs__read" in reg.mcp_names()

    # User registers a tool with the same name through the normal seam
    reg.register(_DummyTool("fs__read"))
    assert "fs__read" in reg.names()
    assert "fs__read" not in reg.mcp_names()


# ── AgentRuntime MCP integration ─────────────────────────────────────────────

async def test_runtime_accepts_mcp_manager_in_aclose_chain():
    """AgentRuntime.aclose must call mcp_manager.aclose if present."""
    sess = FakeMCPSession(tools=[_spec()])
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=fake_session_factory({"fake": sess}),
    )
    await mgr.connect_all()
    registry = default_registry()
    registry.replace_mcp_tools(mgr.tools())

    rt = AgentRuntime(
        model=MODEL, system_prompt=SystemPrompt(),
        tool_registry=registry, cwd=".",
        api_key="dummy",
        mcp_manager=mgr,
    )
    async with rt:
        pass
    # Manager → client → session.aclose was called via the runtime chain
    assert sess.closed_count == 1


async def test_runtime_aclose_order_is_hooks_mcp_registry_provider():
    """Verify the documented teardown order."""
    order: list[str] = []

    class _Hooks:
        async def aclose(self):
            order.append("hooks")
        def before_llm_call(self, *a, **kw): pass
        def before_tool_call(self, *a, **kw): pass
        def after_tool_call(self, *a, **kw): pass

    class _Mgr:
        async def aclose(self):
            order.append("mcp")

    class _Reg:
        async def aclose(self):
            order.append("registry")
        def definitions(self): return []
        def get(self, name): return None
        def names(self): return []

    class _Prov:
        async def aclose(self):
            order.append("provider")
        # Provider Protocol calls; never invoked here.

    rt = AgentRuntime(
        model=MODEL, system_prompt=SystemPrompt(),
        tool_registry=_Reg(), cwd=".",
        api_key="dummy",
        provider=_Prov(), hooks=_Hooks(),
        mcp_manager=_Mgr(),
    )
    await rt.aclose()
    assert order == ["hooks", "mcp", "registry", "provider"]


async def test_runtime_works_without_mcp_manager():
    """Backwards-compat: existing runtimes (no mcp_manager) still work."""
    rt = AgentRuntime(
        model=MODEL, system_prompt=SystemPrompt(),
        tool_registry=default_registry(), cwd=".",
        api_key="dummy",
    )
    async with rt:
        pass  # must not raise even though mcp_manager is None
