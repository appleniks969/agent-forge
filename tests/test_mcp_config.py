"""adapters/mcp/manager.py config surface: the mcp.toml loader (project
overrides global by name; malformed files and entries are skipped, never
raised) and the --mcp-server spec parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.adapters.mcp.manager import (
    MCPServerConfig,
    load_mcp_configs,
    parse_mcp_server_spec,
)


def write_toml(root: Path, text: str) -> Path:
    path = root / ".agent-forge" / "mcp.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def load(tmp_path: Path) -> dict[str, MCPServerConfig]:
    cwd, home = tmp_path / "project", tmp_path / "home"
    return {c.name: c for c in load_mcp_configs(cwd, home=home)}


# --- the loader ------------------------------------------------------------------


def test_missing_files_yield_empty(tmp_path: Path) -> None:
    assert load_mcp_configs(tmp_path / "project", home=tmp_path / "home") == []


def test_project_file_full_schema(tmp_path: Path) -> None:
    write_toml(
        tmp_path / "project",
        """
        [servers.fs]
        command = "mcp-server-filesystem"
        args    = ["/home/me/projects", "/tmp"]
        env     = { GITHUB_TOKEN = "ghp_x" }

        [servers.off]
        command = "mcp-server-other"
        enabled = false
        """,
    )
    configs = load(tmp_path)
    assert configs["fs"] == MCPServerConfig(
        name="fs",
        command="mcp-server-filesystem",
        args=("/home/me/projects", "/tmp"),
        env={"GITHUB_TOKEN": "ghp_x"},
        enabled=True,
    )
    assert configs["off"].enabled is False


def test_project_overrides_global_by_name(tmp_path: Path) -> None:
    write_toml(
        tmp_path / "home",
        """
        [servers.fs]
        command = "global-fs"

        [servers.gh]
        command = "global-gh"
        """,
    )
    write_toml(
        tmp_path / "project",
        """
        [servers.fs]
        command = "project-fs"
        """,
    )
    configs = load(tmp_path)
    assert configs["fs"].command == "project-fs"
    assert configs["gh"].command == "global-gh"  # non-colliding global survives


def test_malformed_file_is_skipped_not_raised(tmp_path: Path) -> None:
    write_toml(tmp_path / "project", "this is [not valid toml")
    write_toml(tmp_path / "home", '[servers.ok]\ncommand = "good"')
    configs = load(tmp_path)
    assert list(configs) == ["ok"]


def test_bad_entries_are_skipped_individually(tmp_path: Path) -> None:
    write_toml(
        tmp_path / "project",
        """
        [servers.no_command]
        args = ["x"]

        [servers.bad_args]
        command = "c"
        args    = [1, 2]

        [servers.bad_env]
        command = "c"
        env     = { KEY = 7 }

        [servers.good]
        command = "c"
        """,
    )
    assert list(load(tmp_path)) == ["good"]


# --- the spec parser ----------------------------------------------------------------


def test_spec_name_command_args() -> None:
    cfg = parse_mcp_server_spec("fs=mcp-server-filesystem /tmp /var")
    assert cfg == MCPServerConfig(
        name="fs", command="mcp-server-filesystem", args=("/tmp", "/var")
    )


def test_spec_quoted_args_survive_tokenisation() -> None:
    cfg = parse_mcp_server_spec("db=psql 'select * from t'")
    assert cfg.command == "psql"
    assert cfg.args == ("select * from t",)


@pytest.mark.parametrize("bad", ["no-equals-here", "=cmd", "name=", " = "])
def test_malformed_specs_raise_value_error(bad: str) -> None:
    with pytest.raises(ValueError, match="--mcp-server"):
        parse_mcp_server_spec(bad)
