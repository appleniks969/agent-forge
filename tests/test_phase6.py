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


# ── Session metadata + listing (auto-title from first user message) ──────────

def test_read_session_meta_extracts_title_from_first_user_message(isolated_home):
    from agent_forge.messages import UserMessage
    from agent_forge.session import (
        append_message, append_metadata, new_id, read_session_meta,
    )
    sid = new_id()
    append_metadata(sid, "claude-sonnet-4-6", "/tmp/proj")
    append_message(sid, UserMessage(content="Spike on session.py UX"))
    meta = read_session_meta(sid)
    assert meta is not None
    assert meta.session_id == sid
    assert meta.cwd == "/tmp/proj"
    assert meta.model == "claude-sonnet-4-6"
    assert meta.title == "Spike on session.py UX"


def test_read_session_meta_truncates_long_titles(isolated_home):
    from agent_forge.messages import UserMessage
    from agent_forge.session import (
        append_message, append_metadata, new_id, read_session_meta,
    )
    sid = new_id()
    append_metadata(sid, "m", "/x")
    long = "word " * 200  # ~1000 chars
    append_message(sid, UserMessage(content=long))
    meta = read_session_meta(sid)
    assert meta is not None
    assert len(meta.title) <= 60
    assert meta.title.endswith("…")


def test_read_session_meta_handles_no_user_messages_yet(isolated_home):
    from agent_forge.session import append_metadata, new_id, read_session_meta
    sid = new_id()
    append_metadata(sid, "m", "/x")
    meta = read_session_meta(sid)
    assert meta is not None
    assert meta.title == ""  # title is "" until a user message is appended


def test_read_session_meta_returns_none_for_missing(isolated_home):
    from agent_forge.session import read_session_meta
    assert read_session_meta("deadbeefdeadbeef") is None


def test_resolve_session_spec_by_index(isolated_home):
    import time
    from agent_forge.messages import UserMessage
    from agent_forge.session import (
        append_message, append_metadata, new_id, resolve_session_spec,
    )
    cwd = "/tmp/proj-r"
    sid1 = new_id(); append_metadata(sid1, "m", cwd); append_message(sid1, UserMessage(content="alpha"))
    time.sleep(0.02)
    sid2 = new_id(); append_metadata(sid2, "m", cwd); append_message(sid2, UserMessage(content="beta"))
    # Newest-first: index 1 = sid2, index 2 = sid1
    assert resolve_session_spec("1", cwd) == sid2
    assert resolve_session_spec("2", cwd) == sid1
    assert resolve_session_spec("3", cwd) is None     # out of range
    assert resolve_session_spec("0", cwd) is None     # 1-based


def test_resolve_session_spec_by_prefix(isolated_home):
    from agent_forge.messages import UserMessage
    from agent_forge.session import (
        append_message, append_metadata, new_id, resolve_session_spec,
    )
    cwd = "/tmp/proj-p"
    sid = new_id(); append_metadata(sid, "m", cwd); append_message(sid, UserMessage(content="x"))
    assert resolve_session_spec(sid, cwd) == sid
    assert resolve_session_spec(sid[:8], cwd) == sid
    assert resolve_session_spec(sid[:4], cwd) == sid
    assert resolve_session_spec("abc", cwd) is None       # too short (<4)
    assert resolve_session_spec("ffffffffff", cwd) is None  # no match


def test_render_session_markdown_basic(isolated_home):
    from agent_forge.messages import (
        AssistantMessage, TextContent, ToolCallContent, ToolResultMessage, UserMessage,
    )
    from agent_forge.session import (
        append_message, append_metadata, new_id, render_session_markdown,
    )
    sid = new_id()
    append_metadata(sid, "claude-sonnet-4-6", "/tmp/showproj")
    append_message(sid, UserMessage(content="hello world"))
    append_message(sid, AssistantMessage(
        content=(
            TextContent(text="hi back"),
            ToolCallContent(id="t1", name="bash", arguments={"command": "ls"}),
        ),
        stop_reason="tool_use",
    ))
    append_message(sid, ToolResultMessage(tool_call_id="t1", content="file.txt", is_error=False))

    md = render_session_markdown(sid)
    assert md is not None
    assert "# hello world" in md            # title from first user msg
    assert f"`{sid}`" in md                 # full sid in header
    assert "/tmp/showproj" in md
    assert "## User" in md
    assert "## Assistant" in md
    assert "hi back" in md
    assert "`tool: bash`" in md
    assert "### Tool result" in md
    assert "file.txt" in md
    assert "_3 messages_" in md             # message count footer


def test_render_session_markdown_missing_returns_none(isolated_home):
    from agent_forge.session import render_session_markdown
    assert render_session_markdown("0123456789abcdef") is None


def test_list_sessions_for_cwd_filters_and_orders_newest_first(isolated_home):
    import time
    from agent_forge.messages import UserMessage
    from agent_forge.session import (
        append_message, append_metadata, list_sessions_for_cwd, new_id,
    )
    cwd_a, cwd_b = "/tmp/proj-a", "/tmp/proj-b"
    sid1 = new_id(); append_metadata(sid1, "m", cwd_a); append_message(sid1, UserMessage(content="first"))
    time.sleep(0.02)
    sid2 = new_id(); append_metadata(sid2, "m", cwd_b); append_message(sid2, UserMessage(content="other cwd"))
    time.sleep(0.02)
    sid3 = new_id(); append_metadata(sid3, "m", cwd_a); append_message(sid3, UserMessage(content="second"))

    metas = list_sessions_for_cwd(cwd_a)
    ids = [m.session_id for m in metas]
    assert ids == [sid3, sid1]  # newest first, cwd_b excluded
    assert metas[0].title == "second"
    assert metas[1].title == "first"
