"""RootedWorkspace containment: ../, absolute, and symlink escapes all rejected."""

from __future__ import annotations

import pytest

from forge.adapters.tools import builtin_tools
from forge.adapters.tools.workspace import RootedWorkspace
from forge.kernel.types import Effects
from forge.ports.tool import WorkspaceEscape


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "file.txt").write_text("hello\n")
    return RootedWorkspace(root)


def test_relative_path_resolves_inside_root(ws):
    assert ws.resolve("sub/file.txt") == ws.root / "sub" / "file.txt"


def test_dot_resolves_to_root(ws):
    assert ws.resolve(".") == ws.root


def test_nonexistent_path_inside_root_resolves(ws):
    assert ws.resolve("new/dir/file.txt") == ws.root / "new" / "dir" / "file.txt"


def test_dotdot_normalizes_when_still_inside(ws):
    assert ws.resolve("sub/../sub/file.txt") == ws.root / "sub" / "file.txt"


def test_dotdot_escape_rejected(ws):
    with pytest.raises(WorkspaceEscape):
        ws.resolve("../outside.txt")


def test_deep_dotdot_escape_rejected(ws):
    with pytest.raises(WorkspaceEscape):
        ws.resolve("sub/../../../etc/passwd")


def test_absolute_path_outside_rejected(ws):
    with pytest.raises(WorkspaceEscape):
        ws.resolve("/etc/passwd")


def test_absolute_path_inside_allowed(ws):
    inside = str(ws.root / "sub" / "file.txt")
    assert ws.resolve(inside) == ws.root / "sub" / "file.txt"


def test_symlink_file_escape_rejected(tmp_path):
    root = tmp_path / "ws2"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret")
    (root / "link.txt").symlink_to(secret)
    ws = RootedWorkspace(root)
    with pytest.raises(WorkspaceEscape):
        ws.resolve("link.txt")


def test_symlink_dir_escape_rejected(tmp_path):
    root = tmp_path / "ws3"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    (root / "ldir").symlink_to(outside, target_is_directory=True)
    ws = RootedWorkspace(root)
    with pytest.raises(WorkspaceEscape):
        ws.resolve("ldir/f.txt")


def test_symlink_inside_root_allowed(tmp_path):
    root = tmp_path / "ws4"
    root.mkdir()
    (root / "real.txt").write_text("x")
    (root / "alias.txt").symlink_to(root / "real.txt")
    ws = RootedWorkspace(root)
    assert ws.resolve("alias.txt") == (root / "real.txt").resolve()


def test_root_reached_through_symlink_is_normalized(tmp_path):
    real = tmp_path / "real_root"
    real.mkdir()
    (real / "f.txt").write_text("x")
    link = tmp_path / "root_link"
    link.symlink_to(real, target_is_directory=True)
    ws = RootedWorkspace(link)
    assert ws.root == real.resolve()
    assert ws.resolve("f.txt") == real.resolve() / "f.txt"


# --- builtin_tools composition ----------------------------------------------


def test_builtin_tools_returns_the_six():
    tools = builtin_tools()
    names = [t.spec.name for t in tools]
    assert names == ["Bash", "Read", "Write", "Edit", "Grep", "Find"]
    assert len(set(names)) == 6


def test_builtin_effects_declarations():
    by_name = {t.spec.name: t.spec.effects for t in builtin_tools()}
    assert by_name["Bash"] == (
        Effects.EXEC | Effects.READ_PATH | Effects.WRITE_PATH | Effects.NETWORK
    )
    assert by_name["Read"] == Effects.READ_PATH
    assert by_name["Write"] == Effects.WRITE_PATH
    assert by_name["Edit"] == Effects.READ_PATH | Effects.WRITE_PATH
    assert by_name["Grep"] == Effects.READ_PATH
    assert by_name["Find"] == Effects.READ_PATH


def test_path_params_marked_for_executor_containment():
    for tool in builtin_tools():
        props = tool.spec.params.get("properties", {})
        if "path" in props:
            assert props["path"].get("format") == "path", tool.spec.name
