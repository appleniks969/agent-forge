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
