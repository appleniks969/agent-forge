"""Tests for Phase 6 additions:
  - ContextBudget.p4_max_bytes / tool_max_bytes
  - session.py index file (latest_session_id O(1) path)
  - PathGuardHook
  - EditTool overlap detection
  - /remember slash command (functional)
"""
from __future__ import annotations

import json

import pytest

from agent_forge.autonomous import PathGuardHook, _CompositeHook, BashGuardHook
from agent_forge.context import ContextBudget, evict_p4
from agent_forge.messages import (
    AssistantMessage, TextContent, ToolCallContent, ToolResult, ToolResultMessage,
)
from agent_forge.tools import EditTool


# ── ContextBudget.p4_max_bytes overrides default ─────────────────────────────

def test_evict_p4_respects_custom_max_bytes():
    long_content = "x" * 4096   # 4 KB
    msg = ToolResultMessage(tool_call_id="t1", content=long_content, timestamp=0)
    # Default 1 KB → evicted
    out = evict_p4([msg])
    assert "evicted" in out[0].content
    # 8 KB cap → kept
    out = evict_p4([msg], max_bytes=8 * 1024)
    assert out[0].content == long_content


def test_context_budget_carries_thresholds():
    b = ContextBudget(keep_recent_tokens=10_000, recency_turns=5,
                      p4_max_bytes=2048, tool_max_bytes=64 * 1024)
    assert b.p4_max_bytes == 2048
    assert b.tool_max_bytes == 64 * 1024


# ── Session index (atomic-ish, falls back on missing) ────────────────────────

@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    yield tmp_path


def test_latest_session_id_uses_index_fast_path(isolated_home):
    from agent_forge.session import (
        append_metadata, latest_session_id, new_id, sessions_dir,
    )
    sid = new_id()
    cwd = "/tmp/projectA"
    append_metadata(sid, "claude-sonnet-4-6", cwd)
    # Index file should now exist
    idx_path = sessions_dir() / "index.json"
    assert idx_path.exists()
    data = json.loads(idx_path.read_text())
    assert data[cwd] == sid
    # Lookup hits the fast path
    assert latest_session_id(cwd) == sid


def test_latest_session_id_fallback_when_index_missing(isolated_home):
    from agent_forge.session import (
        append_metadata, latest_session_id, new_id, sessions_dir,
    )
    sid = new_id()
    cwd = "/tmp/projectB"
    append_metadata(sid, "claude-sonnet-4-6", cwd)
    # Wipe the index — slow scan must still find the session
    (sessions_dir() / "index.json").unlink()
    assert latest_session_id(cwd) == sid


# ── PathGuardHook ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,blocked", [
    ("/etc/passwd",            True),
    ("/usr/local/bin/x",       True),
    ("~/.ssh/id_rsa",          True),
    ("~/.aws/credentials",     True),
    ("src/main.py",            False),
    ("/tmp/foo.txt",           False),
])
@pytest.mark.asyncio
async def test_path_guard_blocks_sensitive_writes(path, blocked):
    h = PathGuardHook()
    call = ToolCallContent(id="1", name="Write", arguments={"path": path, "content": ""})
    decision = await h.before_tool_call(call, turn=1)
    if blocked:
        assert decision is not None and decision.block is True
    else:
        assert decision is None


@pytest.mark.asyncio
async def test_path_guard_ignores_non_write_tools():
    h = PathGuardHook()
    call = ToolCallContent(id="1", name="Read", arguments={"path": "/etc/passwd"})
    assert await h.before_tool_call(call, turn=1) is None


@pytest.mark.asyncio
async def test_path_guard_custom_deny_list():
    h = PathGuardHook(deny_paths=("/sensitive",))
    call = ToolCallContent(id="1", name="Write", arguments={"path": "/sensitive/x"})
    d = await h.before_tool_call(call, turn=1)
    assert d is not None and d.block is True
    # Default-blocked path is NOT in the custom list — should pass
    call2 = ToolCallContent(id="2", name="Write", arguments={"path": "/etc/x"})
    assert await h.before_tool_call(call2, turn=1) is None


# ── _CompositeHook chains hooks; first veto wins ────────────────────────────

@pytest.mark.asyncio
async def test_composite_hook_first_veto_wins():
    chained = _CompositeHook(BashGuardHook(), PathGuardHook())
    bash_call = ToolCallContent(id="1", name="Bash", arguments={"command": "sudo ls"})
    d = await chained.before_tool_call(bash_call, turn=1)
    assert d is not None and d.block is True and "sudo" in (d.reason or "").lower()

    write_call = ToolCallContent(id="2", name="Write", arguments={"path": "/etc/x", "content": ""})
    d2 = await chained.before_tool_call(write_call, turn=1)
    assert d2 is not None and d2.block is True

    benign = ToolCallContent(id="3", name="Bash", arguments={"command": "ls"})
    assert await chained.before_tool_call(benign, turn=1) is None


# ── EditTool overlap detection ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_edit_tool_rejects_identical_old_strings(tmp_path):
    """Two edits targeting the exact same single-occurrence text are ambiguous."""
    p = tmp_path / "f.txt"
    p.write_text("only-once\n")
    tool = EditTool()
    res = await tool.execute({
        "path": "f.txt",
        "edits": [
            {"old_string": "only-once", "new_string": "hi"},
            {"old_string": "only-once", "new_string": "hey"},
        ],
    }, cwd=str(tmp_path))
    assert res.is_error is True
    assert "identical" in res.content.lower()


@pytest.mark.asyncio
async def test_edit_tool_rejects_overlapping_old_strings(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("foobar baz\n")
    tool = EditTool()
    res = await tool.execute({
        "path": "f.txt",
        "edits": [
            {"old_string": "foobar", "new_string": "X"},
            {"old_string": "foo", "new_string": "Y"},
        ],
    }, cwd=str(tmp_path))
    assert res.is_error is True
    assert "overlap" in res.content.lower()


@pytest.mark.asyncio
async def test_edit_tool_allows_disjoint_edits(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("alpha beta gamma\n")
    tool = EditTool()
    res = await tool.execute({
        "path": "f.txt",
        "edits": [
            {"old_string": "alpha", "new_string": "A"},
            {"old_string": "gamma", "new_string": "G"},
        ],
    }, cwd=str(tmp_path))
    assert res.is_error is False
    assert p.read_text() == "A beta G\n"
