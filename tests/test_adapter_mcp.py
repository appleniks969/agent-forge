"""MCP adapter tests: fake sessions only — no SDK, no subprocess, no network.

Covers namespacing, annotation->Effects mapping (incl. the unannotated
default), env allowlisting, failed-server isolation, teardown, reconnect,
and cancel/timeout propagation through MCPTool.run.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.mcp.manager import (
    ENV_ALLOWLIST,
    UNANNOTATED_EFFECTS,
    MCPManager,
    MCPServerConfig,
    MCPTool,
    MCPToolDescriptor,
    ServerStatus,
    effects_from_annotations,
    namespaced_name,
    scrub_env,
)
from forge.kernel.types import Effects, ToolResult
from forge.ports.tool import ToolCtx
from forge.testing import check_tool_contract, check_tool_honors_cancel

# --- fakes -------------------------------------------------------------------


class FakeSession:
    """In-memory MCPSession: scripted descriptors, handlers, failure modes."""

    def __init__(
        self,
        descriptors: tuple[MCPToolDescriptor, ...] = (),
        *,
        handlers: dict[str, Callable[[dict], str]] | None = None,
        raise_on_list: Exception | None = None,
        raise_on_call: Exception | None = None,
        hang_calls: bool = False,
    ) -> None:
        self.descriptors = descriptors
        self.handlers = handlers or {}
        self.raise_on_list = raise_on_list
        self.raise_on_call = raise_on_call
        self.hang_calls = hang_calls
        self.list_calls = 0
        self.calls: list[tuple[str, dict]] = []
        self.call_started = asyncio.Event()
        self.closed = 0

    async def list_tools(self) -> list[MCPToolDescriptor]:
        self.list_calls += 1
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return list(self.descriptors)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        self.call_started.set()
        if self.hang_calls:
            await asyncio.sleep(3600)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.handlers[name](arguments)

    async def aclose(self) -> None:
        self.closed += 1


def factory_for(
    sessions: dict[str, FakeSession],
) -> Callable[[MCPServerConfig], Awaitable[FakeSession]]:
    async def _factory(config: MCPServerConfig) -> FakeSession:
        if config.name not in sessions:
            raise ConnectionError(f"no fake for server {config.name!r}")
        return sessions[config.name]

    return _factory


class StubWorkspace:
    root = Path(".")

    def resolve(self, path: str) -> Path:
        return Path(path)


def ctx(cancel: asyncio.Event | None = None) -> ToolCtx:
    return ToolCtx(ws=StubWorkspace(), cancel=cancel or asyncio.Event())


ECHO = MCPToolDescriptor(
    name="echo",
    description="echoes text back",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
)


def echo_session() -> FakeSession:
    return FakeSession((ECHO,), handlers={"echo": lambda a: a["text"]})


# --- namespacing ---------------------------------------------------------------


def test_namespaced_name_shape() -> None:
    assert namespaced_name("fs", "read") == "fs__read"


async def test_tools_are_namespaced_and_route_native_name() -> None:
    session = echo_session()
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=factory_for({"fake": session}),
    )
    await mgr.connect_all()

    (tool,) = mgr.tools()
    assert tool.spec.name == "fake__echo"
    assert tool.spec.description.startswith("[fake]")
    assert tool.spec.params["properties"] == {"text": {"type": "string"}}

    result = await tool.run({"text": "hi"}, ctx())
    assert isinstance(result, ToolResult)
    assert not result.is_error
    assert result.content == "hi"
    # The server received its native, un-namespaced name.
    assert session.calls == [("echo", {"text": "hi"})]
    await mgr.aclose()


async def test_two_servers_same_tool_name_do_not_collide() -> None:
    mgr = MCPManager(
        [MCPServerConfig(name="a", command="x"), MCPServerConfig(name="b", command="x")],
        session_factory=factory_for({"a": echo_session(), "b": echo_session()}),
    )
    await mgr.connect_all()
    assert sorted(t.spec.name for t in mgr.tools()) == ["a__echo", "b__echo"]
    await mgr.aclose()


def test_duplicate_server_names_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        MCPManager(
            [MCPServerConfig(name="x", command="a"), MCPServerConfig(name="x", command="b")]
        )


# --- annotation -> Effects mapping ----------------------------------------------


def test_unannotated_defaults_to_full_caution() -> None:
    effects = effects_from_annotations(None, None)
    assert effects == UNANNOTATED_EFFECTS
    assert effects == Effects.WRITE_PATH | Effects.EXEC | Effects.EXTERNAL
    assert Effects.EXTERNAL in effects  # EXTERNAL => guard chain defaults to Ask


def test_read_only_hint_maps_to_read_path() -> None:
    assert effects_from_annotations(True, None) == Effects.READ_PATH
    assert effects_from_annotations(True, False) == Effects.READ_PATH


def test_non_destructive_write_maps_to_write_path() -> None:
    assert effects_from_annotations(False, False) == Effects.WRITE_PATH
    assert Effects.EXTERNAL not in effects_from_annotations(False, False)


def test_destructive_or_unspecified_keeps_external() -> None:
    assert effects_from_annotations(False, True) == UNANNOTATED_EFFECTS
    assert effects_from_annotations(None, True) == UNANNOTATED_EFFECTS
    # readOnly explicitly false, destructive unset: MCP defaults destructive true.
    assert effects_from_annotations(False, None) == UNANNOTATED_EFFECTS


async def test_annotations_flow_into_tool_specs() -> None:
    descriptors = (
        MCPToolDescriptor(name="lookup", description="", read_only=True),
        MCPToolDescriptor(name="upsert", description="", read_only=False, destructive=False),
        MCPToolDescriptor(name="nuke", description="", destructive=True),
        MCPToolDescriptor(name="mystery", description=""),
    )
    session = FakeSession(descriptors)
    mgr = MCPManager(
        [MCPServerConfig(name="srv", command="x")],
        session_factory=factory_for({"srv": session}),
    )
    await mgr.connect_all()
    by_name = {t.spec.name: t.spec.effects for t in mgr.tools()}
    assert by_name == {
        "srv__lookup": Effects.READ_PATH,
        "srv__upsert": Effects.WRITE_PATH,
        "srv__nuke": UNANNOTATED_EFFECTS,
        "srv__mystery": UNANNOTATED_EFFECTS,
    }
    await mgr.aclose()


# --- env allowlist ---------------------------------------------------------------


def test_scrub_env_drops_everything_but_allowlist_and_declared() -> None:
    host = {
        "PATH": "/usr/bin",
        "HOME": "/Users/u",
        "LANG": "en_US.UTF-8",
        "AWS_SECRET_ACCESS_KEY": "hunter2",
        "ANTHROPIC_API_KEY": "sk-secret",
        "SSH_AUTH_SOCK": "/tmp/agent",
    }
    out = scrub_env(host, {"GITHUB_TOKEN": "ghp_x"})
    assert out == {
        "PATH": "/usr/bin",
        "HOME": "/Users/u",
        "LANG": "en_US.UTF-8",
        "GITHUB_TOKEN": "ghp_x",
    }


def test_scrub_env_declared_overrides_host_and_missing_allowlist_ok() -> None:
    assert scrub_env({}, {}) == {}
    assert scrub_env({"PATH": "/usr/bin"}, {"PATH": "/custom"}) == {"PATH": "/custom"}
    assert ENV_ALLOWLIST == {"PATH", "HOME", "LANG"}


# --- failed servers never raise ----------------------------------------------------


async def test_failed_server_reports_status_and_others_survive() -> None:
    healthy = echo_session()
    mgr = MCPManager(
        [MCPServerConfig(name="good", command="x"), MCPServerConfig(name="bad", command="x")],
        session_factory=factory_for({"good": healthy}),  # "bad" has no fake => raises
    )
    await mgr.connect_all()  # must not raise
    assert mgr.status() == {
        "good": ServerStatus.CONNECTED,
        "bad": ServerStatus.FAILED,
    }
    assert "bad" in mgr.errors()
    assert "ConnectionError" in mgr.errors()["bad"]
    assert [t.spec.name for t in mgr.tools()] == ["good__echo"]
    await mgr.aclose()


async def test_list_tools_failure_tears_down_half_built_session() -> None:
    session = FakeSession(raise_on_list=RuntimeError("handshake died"))
    mgr = MCPManager(
        [MCPServerConfig(name="s", command="x")],
        session_factory=factory_for({"s": session}),
    )
    await mgr.connect_all()
    assert mgr.status()["s"] is ServerStatus.FAILED
    assert session.closed == 1  # the leaked subprocess problem
    assert mgr.tools() == ()


async def test_default_factory_without_sdk_fails_via_status_not_raise() -> None:
    # No session_factory => the lazy SDK import runs at connect time. The SDK
    # is not installed in this venv, so connect fails — via status, not raise.
    assert "mcp" not in sys.modules  # importing the adapter did not import the SDK
    mgr = MCPManager([MCPServerConfig(name="real", command="definitely-not-a-command")])
    await mgr.connect_all()
    assert mgr.status()["real"] is ServerStatus.FAILED
    assert mgr.errors()["real"]


async def test_disabled_server_is_skipped() -> None:
    factory_calls = 0

    async def counting_factory(config: MCPServerConfig) -> FakeSession:
        nonlocal factory_calls
        factory_calls += 1
        return echo_session()

    mgr = MCPManager(
        [MCPServerConfig(name="off", command="x", enabled=False)],
        session_factory=counting_factory,
    )
    await mgr.connect_all()
    assert factory_calls == 0
    assert mgr.status()["off"] is ServerStatus.DISCONNECTED
    assert mgr.tools() == ()


# --- teardown -----------------------------------------------------------------------


async def test_aclose_closes_every_session_and_is_idempotent() -> None:
    a, b = echo_session(), echo_session()
    mgr = MCPManager(
        [MCPServerConfig(name="a", command="x"), MCPServerConfig(name="b", command="x")],
        session_factory=factory_for({"a": a, "b": b}),
    )
    await mgr.connect_all()
    assert len(mgr.tools()) == 2

    await mgr.aclose()
    assert a.closed == 1 and b.closed == 1
    assert mgr.status() == {"a": ServerStatus.CLOSED, "b": ServerStatus.CLOSED}
    assert mgr.tools() == ()

    await mgr.aclose()  # idempotent: no double-close
    assert a.closed == 1 and b.closed == 1


async def test_stale_tool_after_close_returns_error_result() -> None:
    session = echo_session()
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=factory_for({"fake": session}),
    )
    await mgr.connect_all()
    (tool,) = mgr.tools()
    await mgr.aclose()

    result = await tool.run({"text": "hi"}, ctx())
    assert result.is_error
    assert "not connected" in result.content


# --- reconnect ------------------------------------------------------------------------


async def test_reconnect_recovers_a_previously_failed_server() -> None:
    session = echo_session()
    attempts = 0

    async def flaky_factory(config: MCPServerConfig) -> FakeSession:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("first attempt fails")
        return session

    mgr = MCPManager(
        [MCPServerConfig(name="flaky", command="x")], session_factory=flaky_factory
    )
    await mgr.connect_all()
    assert mgr.status()["flaky"] is ServerStatus.FAILED

    assert await mgr.reconnect("flaky") is True
    assert mgr.status()["flaky"] is ServerStatus.CONNECTED
    assert [t.spec.name for t in mgr.tools()] == ["flaky__echo"]
    await mgr.aclose()


async def test_reconnect_closes_old_session_and_unknown_name_is_false() -> None:
    session = echo_session()
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=factory_for({"fake": session}),
    )
    await mgr.connect_all()
    assert await mgr.reconnect("fake") is True
    assert session.closed == 1  # old session torn down before the new connect
    assert session.list_calls == 2
    assert await mgr.reconnect("nope") is False
    await mgr.aclose()


# --- cancel and timeout propagation ------------------------------------------------------


async def test_cancel_event_interrupts_inflight_call() -> None:
    session = FakeSession((ECHO,), hang_calls=True)
    mgr = MCPManager(
        [MCPServerConfig(name="slow", command="x")],
        session_factory=factory_for({"slow": session}),
    )
    await mgr.connect_all()
    (tool,) = mgr.tools()

    cancel = asyncio.Event()
    run = asyncio.create_task(tool.run({"text": "x"}, ctx(cancel)))
    await asyncio.wait_for(session.call_started.wait(), timeout=2)
    cancel.set()

    result = await asyncio.wait_for(run, timeout=2)
    assert result.is_error
    assert "cancelled" in result.content
    await mgr.aclose()


async def test_call_timeout_returns_error_result() -> None:
    session = FakeSession((ECHO,), hang_calls=True)
    mgr = MCPManager(
        [MCPServerConfig(name="slow", command="x")],
        session_factory=factory_for({"slow": session}),
        call_timeout=0.05,
    )
    await mgr.connect_all()
    (tool,) = mgr.tools()

    result = await asyncio.wait_for(tool.run({"text": "x"}, ctx()), timeout=2)
    assert result.is_error
    assert "timed out after 0.05s" in result.content
    await mgr.aclose()


async def test_server_side_exception_becomes_error_result() -> None:
    session = FakeSession((ECHO,), raise_on_call=ValueError("server exploded"))
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=factory_for({"fake": session}),
    )
    await mgr.connect_all()
    (tool,) = mgr.tools()
    result = await tool.run({"text": "x"}, ctx())
    assert result.is_error
    assert "ValueError" in result.content and "server exploded" in result.content
    await mgr.aclose()


async def test_non_string_payload_is_coerced_to_text() -> None:
    descriptor = MCPToolDescriptor(name="stats", description="", read_only=True)

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return {"count": 3}

    tool = MCPTool("srv", descriptor, call)
    result = await tool.run({}, ctx())
    assert not result.is_error
    assert result.content == '{"count": 3}'


# --- conformance kit ------------------------------------------------------------------------


async def test_mcp_tool_passes_tool_conformance() -> None:
    session = echo_session()
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=factory_for({"fake": session}),
    )
    await mgr.connect_all()
    (tool,) = mgr.tools()
    await check_tool_contract(tool, {"text": "hi"}, ctx())

    pre_cancelled = asyncio.Event()
    pre_cancelled.set()
    await check_tool_honors_cancel(tool, {"text": "hi"}, ctx(pre_cancelled))
    await mgr.aclose()
