"""Round-trip tests for session.py — _msg_to_dict / _dict_to_msg must stay in sync."""
from __future__ import annotations

import json

import pytest

from agent_forge.provider import (
    AssistantMessage, ImageContent, TextContent, ThinkingContent,
    ToolCallContent, ToolResultMessage, UserMessage,
)
from agent_forge.session import (
    _dict_to_msg, _msg_to_dict, append_message, append_metadata, resume_session,
)


def _roundtrip(msg):
    d = _msg_to_dict(msg)
    # Must be JSON-serialisable
    json.dumps(d, ensure_ascii=False)
    return _dict_to_msg(d)


def test_user_message_string_content():
    msg = UserMessage(content="hello", timestamp=1234)
    rt = _roundtrip(msg)
    assert isinstance(rt, UserMessage)
    assert rt.content == "hello"
    assert rt.timestamp == 1234


def test_user_message_structured_content():
    msg = UserMessage(content=(TextContent(text="a"), TextContent(text="b")), timestamp=1)
    rt = _roundtrip(msg)
    assert isinstance(rt, UserMessage)
    assert isinstance(rt.content, tuple)
    assert tuple(c.text for c in rt.content) == ("a", "b")


def test_user_message_with_image_content():
    """Vision inputs piggyback on UserMessage.content as TextContent | ImageContent tuples."""
    msg = UserMessage(
        content=(
            TextContent(text="describe"),
            ImageContent(media_type="image/png", data="iVBORw0KGgo="),
        ),
        timestamp=99,
    )
    rt = _roundtrip(msg)
    assert isinstance(rt, UserMessage)
    assert isinstance(rt.content, tuple) and len(rt.content) == 2
    assert isinstance(rt.content[0], TextContent) and rt.content[0].text == "describe"
    assert isinstance(rt.content[1], ImageContent)
    assert rt.content[1].media_type == "image/png"
    assert rt.content[1].data == "iVBORw0KGgo="


def test_assistant_message_with_thinking_text_and_tool_use():
    msg = AssistantMessage(
        content=(
            ThinkingContent(thinking="hmm", signature="sig"),
            TextContent(text="answer"),
            ToolCallContent(id="t1", name="Read", arguments={"path": "x.txt"}),
        ),
        stop_reason="tool_use",
        usage=None,
        model_id="claude-sonnet-4-6",
        timestamp=42,
    )
    rt = _roundtrip(msg)
    assert isinstance(rt, AssistantMessage)
    assert rt.stop_reason == "tool_use"
    assert rt.model_id == "claude-sonnet-4-6"
    assert len(rt.content) == 3
    assert isinstance(rt.content[0], ThinkingContent)
    assert rt.content[0].signature == "sig"
    assert isinstance(rt.content[1], TextContent)
    assert rt.content[1].text == "answer"
    assert isinstance(rt.content[2], ToolCallContent)
    assert rt.content[2].name == "Read"
    assert rt.content[2].arguments == {"path": "x.txt"}


def test_tool_result_message_string_content():
    msg = ToolResultMessage(tool_call_id="t1", content="output", is_error=False, timestamp=7)
    rt = _roundtrip(msg)
    assert isinstance(rt, ToolResultMessage)
    assert rt.tool_call_id == "t1"
    assert rt.content == "output"
    assert rt.is_error is False


def test_tool_result_message_structured_content():
    msg = ToolResultMessage(
        tool_call_id="t2",
        content=(TextContent(text="hi"), ImageContent(media_type="image/png", data="abc")),
        is_error=True,
        timestamp=8,
    )
    rt = _roundtrip(msg)
    assert isinstance(rt, ToolResultMessage)
    assert rt.is_error is True
    assert isinstance(rt.content, tuple)
    assert isinstance(rt.content[0], TextContent) and rt.content[0].text == "hi"
    assert isinstance(rt.content[1], ImageContent) and rt.content[1].media_type == "image/png"


@pytest.fixture
def isolated_sessions_dir(tmp_path, monkeypatch):
    """Redirect ~/.agent-forge to tmp_path so session writes are isolated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() respects $HOME on POSIX; on every test platform we run, this works.
    yield tmp_path


def test_session_jsonl_resume(isolated_sessions_dir):
    from agent_forge.session import new_id
    sid = new_id()
    append_metadata(sid, "claude-sonnet-4-6", str(isolated_sessions_dir))
    user = UserMessage(content="hi", timestamp=1)
    asst = AssistantMessage(
        content=(TextContent(text="reply"),),
        stop_reason="end_turn", usage=None, model_id="claude-sonnet-4-6", timestamp=2,
    )
    append_message(sid, user)
    append_message(sid, asst)

    resumed = resume_session(sid)
    assert resumed.session_id == sid
    assert len(resumed.messages) == 2
    assert isinstance(resumed.messages[0], UserMessage)
    assert resumed.messages[0].content == "hi"
    assert isinstance(resumed.messages[1], AssistantMessage)
    assert resumed.messages[1].content[0].text == "reply"


def test_session_resume_reattaches_assistant_usage(isolated_sessions_dir):
    """Outer-entry 'usage' must be re-stitched onto AssistantMessage.usage on resume."""
    from agent_forge.messages import TokenUsage
    from agent_forge.session import new_id
    sid = new_id()
    append_metadata(sid, "claude-sonnet-4-6", str(isolated_sessions_dir))
    asst = AssistantMessage(
        content=(TextContent(text="answer"),),
        stop_reason="end_turn",
        usage=TokenUsage(input=42, output=7, cache_read=3, cache_write=1, cost=0.001234),
        model_id="claude-sonnet-4-6",
        timestamp=10,
    )
    append_message(sid, asst, usage=asst.usage)

    resumed = resume_session(sid)
    assert len(resumed.messages) == 1
    rt = resumed.messages[0]
    assert isinstance(rt, AssistantMessage)
    assert rt.usage is not None
    assert rt.usage.input == 42
    assert rt.usage.output == 7
    assert rt.usage.cache_read == 3
    assert rt.usage.cache_write == 1
    assert rt.usage.cost == pytest.approx(0.001234)


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
