"""Front-level MCP wiring: one ToolSource over builtins + a fake MCP server
feeds executor and policy through SessionHandle — the MCP tool is callable,
disappears across a reconnect that drops it, and reappears when restored.
Also: the /mcp slash command and the CLI flag surface."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.mcp.manager import (
    MCPManager,
    MCPServerConfig,
    MCPToolDescriptor,
    ServerStatus,
)
from forge.adapters.tools.workspace import RootedWorkspace
from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.front import commands, oneshot, wiring
from forge.kernel.events import ToolFinished
from forge.kernel.types import ModelOutput, TextBlock, ToolCall, Usage
from forge.policy import StandardPolicy
from forge.testing import FakeProvider, MemoryStore

# --- fakes (same pattern as test_adapter_mcp) -----------------------------------


class FakeSession:
    def __init__(
        self,
        descriptors: tuple[MCPToolDescriptor, ...],
        *,
        handlers: dict[str, Callable[[dict], str]] | None = None,
    ) -> None:
        self.descriptors = descriptors
        self.handlers = handlers or {}
        self.closed = 0

    async def list_tools(self) -> list[MCPToolDescriptor]:
        return list(self.descriptors)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return self.handlers[name](arguments)

    async def aclose(self) -> None:
        self.closed += 1


# read_only=True -> Effects.READ_PATH: the standard guard chain allows it,
# so the turn needs no permission round-trip.
ECHO = MCPToolDescriptor(
    name="echo",
    description="echoes text back",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    read_only=True,
)


class SwitchableFactory:
    """Each (re)connect builds a session from the CURRENT descriptor set."""

    def __init__(self) -> None:
        self.descriptors: tuple[MCPToolDescriptor, ...] = (ECHO,)

    async def __call__(self, config: MCPServerConfig) -> FakeSession:
        return FakeSession(self.descriptors, handlers={"echo": lambda a: a["text"]})


async def connected_manager(factory: SwitchableFactory | None = None) -> MCPManager:
    manager = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=factory or SwitchableFactory(),
    )
    await manager.connect_all()
    return manager


def out_call(cid: str) -> ModelOutput:
    return ModelOutput(
        blocks=(ToolCall(cid, "fake__echo", {"text": "hi"}),), usage=Usage(10, 5)
    )


def out_text(text: str) -> ModelOutput:
    return ModelOutput(blocks=(TextBlock(text),), usage=Usage(10, 5))


# --- the source through SessionHandle, across reconnects ---------------------------


async def test_mcp_tool_disappears_and_reappears_across_reconnect(
    tmp_path: Path,
) -> None:
    factory = SwitchableFactory()
    manager = await connected_manager(factory)
    source = wiring.build_tool_source(manager)
    names = {t.spec.name for t in source.all()}
    assert "fake__echo" in names and "Bash" in names  # builtins + MCP, one source

    provider = FakeProvider(
        [out_call("c1"), out_text("t1"), out_call("c2"), out_text("t2"),
         out_call("c3"), out_text("t3")]
    )
    ws = RootedWorkspace(tmp_path)
    policy = StandardPolicy(
        model="m",
        context_tokens=200_000,
        tools=lambda: tuple(t.spec for t in source.all()),
        ws_root=ws.root,
    )
    store = MemoryStore()
    handle = SessionHandle.open(
        store,
        provider=provider,
        executor=ToolExecutor(source, ws),
        policy=policy,
        asker=oneshot.StaticAsker(allow=True),
    )

    await handle.submit("turn 1")  # tool present

    factory.descriptors = ()  # the server lost the tool
    assert await manager.reconnect("fake") is True
    assert "fake__echo" not in {t.spec.name for t in source.all()}
    await handle.submit("turn 2")

    factory.descriptors = (ECHO,)  # ...and got it back
    assert await manager.reconnect("fake") is True
    await handle.submit("turn 3")
    await handle.close()
    await manager.aclose()

    finished = {
        env.body.result.call_id: env.body.result
        for env in store.replay()
        if isinstance(env.body, ToolFinished)
    }
    assert not finished["c1"].is_error and finished["c1"].content == "hi"
    assert finished["c2"].is_error and "unknown tool" in finished["c2"].content
    assert not finished["c3"].is_error and finished["c3"].content == "hi"

    # The prompt thunk re-resolved per build: turn 2's request lost the tool.
    tools_per_turn = [
        {t.name for t in req.tools} for req in provider.requests
    ]
    assert "fake__echo" in tools_per_turn[0]
    assert "fake__echo" not in tools_per_turn[2]  # first request of turn 2
    assert "fake__echo" in tools_per_turn[4]  # first request of turn 3


# --- the /mcp slash command --------------------------------------------------------


def make_ctx(tmp_path: Path, manager: MCPManager | None) -> commands.CommandContext:
    ws = RootedWorkspace(tmp_path)
    handle = SessionHandle.open(
        MemoryStore(),
        provider=FakeProvider([]),
        executor=ToolExecutor((), ws),
        policy=StandardPolicy(model="m", context_tokens=200_000, tools=(), ws_root=ws.root),
        asker=oneshot.StaticAsker(allow=False),
    )
    return commands.CommandContext(session=handle, model="m", mcp=manager)


def test_mcp_command_without_manager(tmp_path: Path) -> None:
    outcome = commands.dispatch("/mcp", make_ctx(tmp_path, None))
    assert "no servers configured" in outcome.text


async def test_mcp_command_status_table(tmp_path: Path) -> None:
    manager = await connected_manager()
    text = commands.dispatch("/mcp", make_ctx(tmp_path, manager)).text
    assert "fake" in text
    assert ServerStatus.CONNECTED.value in text
    assert "1 tools" in text
    await manager.aclose()


async def test_mcp_command_reconnect_swaps_tools_in_the_source(tmp_path: Path) -> None:
    factory = SwitchableFactory()
    manager = await connected_manager(factory)
    source = wiring.build_tool_source(manager)
    assert source.get("fake__echo") is not None

    ctx = make_ctx(tmp_path, manager)
    factory.descriptors = ()
    outcome = commands.dispatch("/mcp reconnect fake", ctx)
    assert outcome.action is not None
    assert "connected" in await outcome.action()
    assert source.get("fake__echo") is None  # the reconnect reached the source

    assert "usage" in commands.dispatch("/mcp reconnect", ctx).text
    assert "unknown server" in commands.dispatch("/mcp reconnect nope", ctx).text
    assert "unknown subcommand" in commands.dispatch("/mcp bogus", ctx).text
    await manager.aclose()


async def test_repl_awaits_the_mcp_reconnect_action(tmp_path: Path) -> None:
    import io

    from forge.front import repl

    manager = await connected_manager()
    out = io.StringIO()

    async def make_session() -> SessionHandle:
        return make_ctx(tmp_path, manager).session

    lines = iter(["/mcp", "/mcp reconnect fake", "/quit"])
    rc = await repl.run_repl(
        make_session,
        model="m",
        input_fn=lambda _: next(lines),
        out=out,
        mcp=manager,
    )
    text = out.getvalue()
    assert rc == 0
    assert "MCP servers:" in text  # the status table
    assert "fake: connected" in text  # the awaited reconnect action's result
    await manager.aclose()


# --- the CLI flag surface ------------------------------------------------------------


def parse_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> wiring.Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # isolate ~/.agent-forge
    return wiring.load_settings(wiring._parser().parse_args(argv))


def write_project_toml(tmp_path: Path, text: str) -> None:
    path = tmp_path / ".agent-forge" / "mcp.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_settings_auto_enable_from_project_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_project_toml(tmp_path, '[servers.fs]\ncommand = "fs-cmd"')
    settings = parse_settings(monkeypatch, tmp_path, [])
    assert [c.name for c in settings.mcp_configs] == ["fs"]


def test_settings_no_mcp_skips_files_but_keeps_cli_specs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_project_toml(tmp_path, '[servers.fs]\ncommand = "fs-cmd"')
    settings = parse_settings(
        monkeypatch, tmp_path, ["--no-mcp", "--mcp-server", "gh=gh-cmd --flag"]
    )
    assert [c.name for c in settings.mcp_configs] == ["gh"]
    assert settings.mcp_configs[0].args == ("--flag",)


def test_settings_cli_spec_overrides_file_by_name_and_is_repeatable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_project_toml(tmp_path, '[servers.fs]\ncommand = "from-file"')
    settings = parse_settings(
        monkeypatch,
        tmp_path,
        ["--mcp-server", "fs=from-cli", "--mcp-server", "db=db-cmd"],
    )
    by_name = {c.name: c for c in settings.mcp_configs}
    assert by_name["fs"].command == "from-cli"
    assert by_name["db"].command == "db-cmd"


def test_settings_default_is_no_mcp_configs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert parse_settings(monkeypatch, tmp_path, []).mcp_configs == ()


def test_malformed_mcp_server_flag_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        sys, "argv", ["forge", "run", "--provider", "fake", "-p", "x", "--mcp-server", "garbage"]
    )
    assert wiring.main() == 2
    assert "--mcp-server" in capsys.readouterr().err


def test_main_run_survives_unconnectable_mcp_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The SDK is not installed and the command does not exist: the server
    # lands in FAILED, builtins still work, teardown still runs — exit 0.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FORGE_SESSIONS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["forge", "run", "--provider", "fake", "-p", "hello", "--json",
         "--mcp-server", "ghost=definitely-not-a-command"],
    )
    assert wiring.main() == 0
    record = json.loads(capsys.readouterr().out)
    assert record["outcome"] == "ok"
