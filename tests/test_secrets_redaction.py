"""Tests for write-time secrets redaction in WriteTool / EditTool."""
from __future__ import annotations

import json

import pytest

from agent_forge.messages import (
    AssistantMessage, ImageContent, TextContent, ThinkingContent,
    TokenUsage, ToolCallContent, ToolResultMessage, UserMessage,
)
from agent_forge.session import (
    Redactor, append_message, append_metadata, new_id, redact_secrets,
    resume_session, sessions_dir,
)


# ── isolated_home fixture ────────────────────────────────────────────────────

@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── redact_secrets default patterns ──────────────────────────────────────────

def test_redact_secrets_masks_anthropic_key():
    msg = UserMessage(content="my key is sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx-123456")
    out = redact_secrets(msg)
    assert "sk-ant-" not in out.content
    assert "[REDACTED]" in out.content
    assert "my key is" in out.content


def test_redact_secrets_masks_openai_style_key():
    msg = UserMessage(content="OPENAI_KEY=sk-1234567890abcdefghij_klmn-test")
    out = redact_secrets(msg)
    assert "sk-1234567890" not in out.content
    assert "[REDACTED]" in out.content


def test_redact_secrets_masks_github_tokens():
    for prefix in ("ghp_", "gho_", "ghs_", "ghu_", "ghr_"):
        token = f"{prefix}abcdef0123456789abcdef0123456789"
        msg = UserMessage(content=f"token={token}")
        out = redact_secrets(msg)
        assert token not in out.content, f"{prefix} not redacted"


def test_redact_secrets_masks_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxIiwibmFtZSI6IngifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    msg = UserMessage(content=f"Authorization: Bearer {jwt}")
    out = redact_secrets(msg)
    assert "eyJhbGci" not in out.content
    assert "[REDACTED]" in out.content


def test_redact_secrets_preserves_non_matching_content():
    msg = UserMessage(content="hello world, nothing secret here")
    out = redact_secrets(msg)
    # Object identity: no copy when nothing changed.
    assert out is msg


def test_redact_secrets_handles_tool_result_message():
    tr = ToolResultMessage(
        tool_call_id="x",
        content="exported AWS_KEY: sk-abcdefghij1234567890_test from env",
    )
    out = redact_secrets(tr)
    assert "sk-abcdef" not in out.content
    assert "[REDACTED]" in out.content


def test_redact_secrets_handles_assistant_text_blocks():
    asst = AssistantMessage(
        content=(
            TextContent(text="Here is the key: sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXX"),
            ToolCallContent(id="t1", name="Bash", arguments={"command": "ls"}),
        ),
        usage=TokenUsage(),
    )
    out = redact_secrets(asst)
    assert isinstance(out, AssistantMessage)
    text_block = out.content[0]
    assert isinstance(text_block, TextContent)
    assert "sk-ant" not in text_block.text
    assert "[REDACTED]" in text_block.text
    # ToolCallContent untouched
    assert out.content[1].name == "Bash"


def test_redact_secrets_handles_structured_user_message():
    msg = UserMessage(content=(
        TextContent(text="image with key: sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXX"),
        ImageContent(media_type="image/png", data="b64..."),
    ))
    out = redact_secrets(msg)
    assert isinstance(out.content, tuple)
    assert "sk-ant" not in out.content[0].text
    # ImageContent passes through unchanged
    assert isinstance(out.content[1], ImageContent)


# ── append_message integration ──────────────────────────────────────────────

def test_append_message_writes_redacted_content_to_disk(isolated_home):
    sid = new_id()
    append_metadata(sid, "m", "/tmp/proj")

    secret = "sk-ant-api03-DangerousPrivateKey1234567890ABC"
    msg = UserMessage(content=f"key is {secret}")
    append_message(sid, msg, redactor=redact_secrets)

    # Verify on-disk JSONL is redacted
    path = sessions_dir() / f"{sid}.jsonl"
    text = path.read_text()
    assert secret not in text
    assert "[REDACTED]" in text

    # Verify the original in-memory msg is untouched
    assert secret in msg.content


def test_append_message_without_redactor_writes_raw(isolated_home):
    """When redactor is None (default), nothing is masked."""
    sid = new_id()
    append_metadata(sid, "m", "/tmp/proj")

    secret = "sk-ant-api03-Verbatim1234567890ABCDEF"
    append_message(sid, UserMessage(content=f"key={secret}"))

    path = sessions_dir() / f"{sid}.jsonl"
    text = path.read_text()
    assert secret in text


def test_redacted_message_roundtrips_through_resume(isolated_home):
    """Resume reads back the redacted form — that's the point: persistence is truth."""
    sid = new_id()
    append_metadata(sid, "m", "/tmp/proj")
    append_message(
        sid,
        UserMessage(content="leak: ghp_abcdef0123456789abcdef0123456789X"),
        redactor=redact_secrets,
    )

    resumed = resume_session(sid)
    assert resumed is not None
    assert len(resumed.messages) == 1
    assert "ghp_" not in resumed.messages[0].content
    assert "[REDACTED]" in resumed.messages[0].content


# ── Custom Redactor composition ──────────────────────────────────────────────

def test_custom_redactor_can_be_used():
    """The Redactor type is a plain Callable — users can plug in any function."""
    def mask_password(msg):
        import dataclasses
        if isinstance(msg, UserMessage) and isinstance(msg.content, str):
            return dataclasses.replace(msg, content=msg.content.replace("hunter2", "***"))
        return msg

    out: Redactor = mask_password
    result = out(UserMessage(content="login with hunter2"))
    assert "***" in result.content
    assert "hunter2" not in result.content
