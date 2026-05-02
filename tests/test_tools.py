"""Smoke tests for tools — sandbox and EditTool fuzzy matching."""
from __future__ import annotations

import pytest

from agent_forge.tools import (
    BashTool, EditTool, ReadTool, WriteTool, _sandbox, default_registry,
)


# ── Sandbox ──────────────────────────────────────────────────────────────────

def test_sandbox_accepts_relative_path(tmp_path):
    target = tmp_path / "sub" / "file.txt"
    target.parent.mkdir()
    target.write_text("x")
    resolved = _sandbox("sub/file.txt", str(tmp_path))
    assert resolved.endswith("sub/file.txt")


def test_sandbox_rejects_traversal(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        _sandbox("../outside.txt", str(tmp_path))


# ── ReadTool ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_tool_returns_numbered_lines(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("first\nsecond\nthird\n")
    res = await ReadTool().execute({"path": "a.txt"}, cwd=str(tmp_path))
    assert not res.is_error
    assert "1\tfirst" in res.content
    assert "3\tthird" in res.content


# ── WriteTool ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_tool_creates_file(tmp_path):
    res = await WriteTool().execute(
        {"path": "out/new.txt", "content": "abc\nxyz\n"}, cwd=str(tmp_path),
    )
    assert not res.is_error
    assert (tmp_path / "out" / "new.txt").read_text() == "abc\nxyz\n"


# ── EditTool: exact match, fuzzy, and multi-edit ─────────────────────────────

@pytest.mark.asyncio
async def test_edit_tool_exact_replace(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("alpha beta gamma")
    res = await EditTool().execute(
        {"path": "x.txt", "old_string": "beta", "new_string": "BETA"},
        cwd=str(tmp_path),
    )
    assert not res.is_error, res.content
    assert p.read_text() == "alpha BETA gamma"


@pytest.mark.asyncio
async def test_edit_tool_fuzzy_crlf(tmp_path):
    p = tmp_path / "x.txt"
    # File has CRLF line endings
    p.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    # old_string uses LF (typical of LLM output)
    res = await EditTool().execute(
        {"path": "x.txt", "old_string": "line2\n", "new_string": "LINE2\n"},
        cwd=str(tmp_path),
    )
    assert not res.is_error, res.content
    assert "LINE2" in p.read_text()


@pytest.mark.asyncio
async def test_edit_tool_rejects_ambiguous_without_replace_all(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("foo foo foo")
    res = await EditTool().execute(
        {"path": "x.txt", "old_string": "foo", "new_string": "bar"},
        cwd=str(tmp_path),
    )
    assert res.is_error
    assert "3 times" in res.content or "replace_all" in res.content


@pytest.mark.asyncio
async def test_edit_tool_multi_edit_atomic(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("apple banana cherry")
    res = await EditTool().execute(
        {
            "path": "x.txt",
            "edits": [
                {"old_string": "apple", "new_string": "APPLE"},
                {"old_string": "cherry", "new_string": "CHERRY"},
            ],
        },
        cwd=str(tmp_path),
    )
    assert not res.is_error, res.content
    assert p.read_text() == "APPLE banana CHERRY"


# ── default_registry has the 6 expected tools ────────────────────────────────

def test_default_registry_has_six_tools():
    reg = default_registry()
    assert set(reg.names()) == {"Bash", "Read", "Write", "Edit", "Grep", "Find"}
