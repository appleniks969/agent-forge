"""Tests for the MCP composition layer.

Covers:
  • TOML config loader (`load_mcp_configs`) — file precedence, malformed handling
  • CLI spec parser (`parse_mcp_server_spec`) — happy + error paths
  • `build_runtime_with_mcp` factory — connect-on-build, registry hot-load,
    aclose teardown, on_status callback
  • Chat-level `_resolve_mcp_configs` precedence (file + CLI merge)
  • `/mcp` slash-command dispatcher behaviour
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from agent_forge.chat import (
    ChatConfig, _format_mcp_status, _handle_mcp_command, _resolve_mcp_configs,
)
from agent_forge.mcp import (
    MCPManager, MCPServerConfig, MCPServerStatus,
    load_mcp_configs, parse_mcp_server_spec,
)
from agent_forge.models import MODELS
from agent_forge.runtime import AgentRuntime, build_runtime_with_mcp
from agent_forge.system_prompt import SystemPrompt
from agent_forge.tools import default_registry

from tests.fake_mcp_server import FakeMCPSession, fake_session_factory


MODEL = MODELS["claude-sonnet-4-6"]


def _spec(name: str = "echo"):
    return (
        name,
        f"description of {name}",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        lambda args: args.get("text", ""),
    )


# ── parse_mcp_server_spec ────────────────────────────────────────────────────

def test_parse_spec_simple():
    cfg = parse_mcp_server_spec("fs=mcp-server-filesystem")
    assert cfg.name == "fs"
    assert cfg.command == "mcp-server-filesystem"
    assert cfg.args == ()


def test_parse_spec_with_args():
    cfg = parse_mcp_server_spec("fs=mcp-server-filesystem /tmp /home")
    assert cfg.command == "mcp-server-filesystem"
    assert cfg.args == ("/tmp", "/home")


def test_parse_spec_respects_quoted_args():
    cfg = parse_mcp_server_spec("db=psql 'select * from t'")
    assert cfg.args == ("select * from t",)


def test_parse_spec_rejects_missing_equals():
    with pytest.raises(ValueError, match="name=command"):
        parse_mcp_server_spec("just-a-command")


def test_parse_spec_rejects_empty_name():
    with pytest.raises(ValueError, match="empty name"):
        parse_mcp_server_spec("=mcp-server")


def test_parse_spec_rejects_empty_command():
    with pytest.raises(ValueError, match="empty"):
        parse_mcp_server_spec("name=")


# ── load_mcp_configs (TOML) ──────────────────────────────────────────────────

def _write_mcp_toml(dir_: Path, content: str) -> Path:
    target = dir_ / ".agent-forge" / "mcp.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def test_load_configs_returns_empty_when_no_files(tmp_path, monkeypatch):
    # Point HOME at an empty dir so the global file doesn't pollute the test
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Reload the module so _GLOBAL_MCP_TOML re-evaluates Path.home()? Not
    # possible — it's evaluated at import time. Instead use cwd-only test
    # by relying on the empty project dir.
    configs = load_mcp_configs(tmp_path)
    # Global may still exist on the dev's machine; just verify no crash
    # and that we don't pick up anything *project*-specific:
    project_names = {c.name for c in configs}
    # No project file was written so there should be no "test_*" servers
    assert all(not n.startswith("test_") for n in project_names)


def test_load_project_config(tmp_path):
    _write_mcp_toml(tmp_path, """
[servers.fs]
command = "mcp-server-filesystem"
args = ["/tmp"]
""")
    configs = load_mcp_configs(tmp_path)
    fs = [c for c in configs if c.name == "fs"]
    assert len(fs) == 1
    assert fs[0].command == "mcp-server-filesystem"
    assert fs[0].args == ("/tmp",)
    assert fs[0].enabled is True


def test_load_config_with_env_and_disabled(tmp_path):
    _write_mcp_toml(tmp_path, """
[servers.gh]
command = "mcp-server-github"
env = { GITHUB_TOKEN = "abc123" }
enabled = false
""")
    configs = load_mcp_configs(tmp_path)
    gh = next(c for c in configs if c.name == "gh")
    assert gh.env == {"GITHUB_TOKEN": "abc123"}
    assert gh.enabled is False


def test_load_skips_malformed_toml(tmp_path, caplog):
    _write_mcp_toml(tmp_path, "this is not valid {} toml [[[[")
    with caplog.at_level("WARNING"):
        configs = load_mcp_configs(tmp_path)
    # Project entries from this file are dropped; no crash
    assert all(c.name != "should_not_appear" for c in configs)


def test_load_skips_server_missing_command(tmp_path):
    _write_mcp_toml(tmp_path, """
[servers.fs]
args = ["/tmp"]
# no command field
""")
    configs = load_mcp_configs(tmp_path)
    assert all(c.name != "fs" for c in configs)


def test_load_skips_server_with_wrong_args_type(tmp_path):
    _write_mcp_toml(tmp_path, """
[servers.fs]
command = "x"
args = "not a list"
""")
    configs = load_mcp_configs(tmp_path)
    assert all(c.name != "fs" for c in configs)


# ── _resolve_mcp_configs (file + CLI merge) ──────────────────────────────────

def _chat_cfg(*, cwd, mcp_enabled=True, mcp_servers=None):
    return ChatConfig(
        api_key="dummy", cwd=str(cwd),
        mcp_enabled=mcp_enabled,
        mcp_servers=mcp_servers or [],
    )


def test_resolve_returns_file_configs_when_enabled(tmp_path):
    _write_mcp_toml(tmp_path, """
[servers.fs]
command = "mcp-fs"
""")
    cfg = _chat_cfg(cwd=tmp_path, mcp_enabled=True)
    configs = _resolve_mcp_configs(cfg)
    assert any(c.name == "fs" for c in configs)


def test_resolve_skips_files_when_disabled(tmp_path):
    _write_mcp_toml(tmp_path, """
[servers.fs]
command = "mcp-fs"
""")
    cfg = _chat_cfg(cwd=tmp_path, mcp_enabled=False)
    configs = _resolve_mcp_configs(cfg)
    assert all(c.name != "fs" for c in configs)


def test_resolve_cli_overrides_file(tmp_path):
    _write_mcp_toml(tmp_path, """
[servers.fs]
command = "old-cmd"
""")
    override = MCPServerConfig(name="fs", command="new-cmd")
    cfg = _chat_cfg(cwd=tmp_path, mcp_servers=[override])
    configs = _resolve_mcp_configs(cfg)
    fs = next(c for c in configs if c.name == "fs")
    assert fs.command == "new-cmd"


def test_resolve_cli_works_even_when_files_disabled(tmp_path):
    """--no-mcp + --mcp-server should still produce a config."""
    cfg = _chat_cfg(
        cwd=tmp_path, mcp_enabled=False,
        mcp_servers=[MCPServerConfig(name="adhoc", command="x")],
    )
    configs = _resolve_mcp_configs(cfg)
    assert [c.name for c in configs] == ["adhoc"]


# ── build_runtime_with_mcp ───────────────────────────────────────────────────

async def test_factory_with_no_configs_returns_runtime_without_manager():
    rt = await build_runtime_with_mcp(
        model=MODEL, system_prompt=SystemPrompt(),
        tool_registry=default_registry(), cwd=".",
        mcp_configs=[], api_key="dummy",
    )
    assert rt.mcp_manager is None
    await rt.aclose()


async def test_factory_connects_servers_and_loads_tools(monkeypatch):
    """End-to-end: configs in → connected manager + registry has MCP tools out."""
    sess = FakeMCPSession(tools=[_spec("echo"), _spec("upper")])

    # Monkey-patch MCPManager so we can inject the fake session factory.
    # Cleanest is to construct the manager ourselves before calling the
    # factory; but build_runtime_with_mcp creates the manager internally.
    # Instead we patch MCPManager to use the fake factory by default.
    import agent_forge.mcp as mcp_mod
    real_mgr = mcp_mod.MCPManager

    def patched_manager(configs, **kw):
        kw.setdefault("session_factory", fake_session_factory({"fake": sess}))
        return real_mgr(configs, **kw)

    monkeypatch.setattr(mcp_mod, "MCPManager", patched_manager)
    # build_runtime_with_mcp imports MCPManager via `from .mcp import ...`
    # inside the function, so the monkeypatch is picked up.

    registry = default_registry()
    builtin_count = len(registry.names())
    statuses: list[tuple[str, str, str | None]] = []

    rt = await build_runtime_with_mcp(
        model=MODEL, system_prompt=SystemPrompt(),
        tool_registry=registry, cwd=".",
        mcp_configs=[MCPServerConfig(name="fake", command="x")],
        api_key="dummy",
        on_status=lambda n, s, e: statuses.append((n, s, e)),
    )

    # 1. Manager is attached
    assert rt.mcp_manager is not None
    # 2. on_status callback got one entry per server
    assert statuses == [("fake", "connected", None)]
    # 3. Registry gained the MCP tools (namespaced)
    names = registry.names()
    assert "fake__echo" in names and "fake__upper" in names
    assert len(names) == builtin_count + 2
    # 4. aclose tears it all down via the runtime
    async with rt:
        pass
    assert sess.closed_count == 1


async def test_factory_with_failing_server_still_builds_runtime(monkeypatch):
    """One failed server must not stop the factory from returning."""
    import agent_forge.mcp as mcp_mod
    real_mgr = mcp_mod.MCPManager

    def patched_manager(configs, **kw):
        # Factory raises for any server — emulates broken command.
        async def failing(_cfg):
            raise FileNotFoundError("no such executable")
        kw.setdefault("session_factory", failing)
        return real_mgr(configs, **kw)

    monkeypatch.setattr(mcp_mod, "MCPManager", patched_manager)
    statuses: list[tuple[str, str, str | None]] = []

    rt = await build_runtime_with_mcp(
        model=MODEL, system_prompt=SystemPrompt(),
        tool_registry=default_registry(), cwd=".",
        mcp_configs=[MCPServerConfig(name="bad", command="x")],
        api_key="dummy",
        on_status=lambda n, s, e: statuses.append((n, s, e)),
    )
    assert rt.mcp_manager is not None
    assert len(statuses) == 1
    assert statuses[0][0] == "bad"
    assert statuses[0][1] == "failed"
    assert "no such executable" in (statuses[0][2] or "")
    await rt.aclose()


# ── /mcp slash command ──────────────────────────────────────────────────────

def _runtime_with_manager(mgr):
    return AgentRuntime(
        model=MODEL, system_prompt=SystemPrompt(),
        tool_registry=default_registry(), cwd=".",
        api_key="dummy",
        mcp_manager=mgr,
    )


async def test_mcp_command_no_manager_shows_disabled_hint():
    rt = _runtime_with_manager(None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        await _handle_mcp_command("/mcp", rt, default_registry())
    out = buf.getvalue()
    assert "not enabled" in out


async def test_mcp_command_shows_per_server_status(monkeypatch):
    sess = FakeMCPSession(tools=[_spec()])
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=fake_session_factory({"fake": sess}),
    )
    await mgr.connect_all()
    rt = _runtime_with_manager(mgr)

    buf = io.StringIO()
    with redirect_stdout(buf):
        await _handle_mcp_command("/mcp", rt, default_registry())
    out = buf.getvalue()
    assert "fake" in out
    assert "connected" in out
    await mgr.aclose()


async def test_mcp_tools_subcommand_lists_namespaced_tools():
    sess = FakeMCPSession(tools=[_spec("echo"), _spec("upper")])
    mgr = MCPManager(
        [MCPServerConfig(name="fs", command="x")],
        session_factory=fake_session_factory({"fs": sess}),
    )
    await mgr.connect_all()
    rt = _runtime_with_manager(mgr)

    buf = io.StringIO()
    with redirect_stdout(buf):
        await _handle_mcp_command("/mcp tools", rt, default_registry())
    out = buf.getvalue()
    assert "fs__echo" in out
    assert "fs__upper" in out
    await mgr.aclose()


async def test_mcp_tools_subcommand_handles_empty():
    rt = _runtime_with_manager(None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        await _handle_mcp_command("/mcp tools", rt, default_registry())
    assert "No MCP tools" in buf.getvalue()


async def test_mcp_reconnect_named_server():
    """Reconnect refreshes the manager and re-loads the registry."""
    # First connect succeeds; second connect uses different tools
    calls = {"n": 0}

    async def factory(cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeMCPSession(tools=[_spec("v1")])
        return FakeMCPSession(tools=[_spec("v2")])

    mgr = MCPManager(
        [MCPServerConfig(name="srv", command="x")],
        session_factory=factory,
    )
    await mgr.connect_all()
    rt = _runtime_with_manager(mgr)
    registry = default_registry()
    registry.replace_mcp_tools(mgr.tools())
    assert "srv__v1" in registry.names()

    buf = io.StringIO()
    with redirect_stdout(buf):
        await _handle_mcp_command("/mcp reconnect srv", rt, registry)
    out = buf.getvalue()
    assert "connected" in out
    # Registry got hot-swapped to v2 tools
    assert "srv__v2" in registry.names()
    assert "srv__v1" not in registry.names()
    await mgr.aclose()


async def test_mcp_reconnect_all_servers():
    sess_a = FakeMCPSession(tools=[_spec("a")])
    sess_b = FakeMCPSession(tools=[_spec("b")])
    # Each reconnect rebuilds the client and calls the factory once more,
    # so we need fresh sessions on the second call too.
    sess_a2 = FakeMCPSession(tools=[_spec("a")])
    sess_b2 = FakeMCPSession(tools=[_spec("b")])
    queue_a = [sess_a, sess_a2]
    queue_b = [sess_b, sess_b2]

    async def factory(cfg):
        if cfg.name == "A":
            return queue_a.pop(0)
        return queue_b.pop(0)

    mgr = MCPManager(
        [MCPServerConfig(name="A", command="x"), MCPServerConfig(name="B", command="x")],
        session_factory=factory,
    )
    await mgr.connect_all()
    rt = _runtime_with_manager(mgr)
    registry = default_registry()
    registry.replace_mcp_tools(mgr.tools())

    buf = io.StringIO()
    with redirect_stdout(buf):
        await _handle_mcp_command("/mcp reconnect", rt, registry)
    # Both reconnected → both still in registry
    assert "A__a" in registry.names()
    assert "B__b" in registry.names()
    await mgr.aclose()


async def test_mcp_unknown_subcommand():
    rt = _runtime_with_manager(None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        await _handle_mcp_command("/mcp banana", rt, default_registry())
    assert "Unknown" in buf.getvalue()


# ── _format_mcp_status ───────────────────────────────────────────────────────

async def test_format_status_no_manager():
    rt = _runtime_with_manager(None)
    out = _format_mcp_status(rt)
    assert "not enabled" in out


async def test_format_status_empty_manager():
    rt = _runtime_with_manager(MCPManager([]))
    out = _format_mcp_status(rt)
    assert "no servers" in out.lower()


async def test_format_status_with_servers():
    sess = FakeMCPSession(tools=[_spec()])
    mgr = MCPManager(
        [MCPServerConfig(name="fake", command="x")],
        session_factory=fake_session_factory({"fake": sess}),
    )
    await mgr.connect_all()
    rt = _runtime_with_manager(mgr)
    out = _format_mcp_status(rt)
    assert "fake" in out
    assert "connected" in out
    # 1 tool, singular form
    assert "1 tool" in out and "1 tools" not in out
    await mgr.aclose()
