"""Tests for wiki/ratchet/ — bundle builder, skill loader, runner."""
from __future__ import annotations

import pytest

from agent_forge.messages import (
    AssistantMessage, TextContent, ToolCallContent,
    ToolResultMessage, UserMessage,
)
from agent_forge.session import (
    append_message, append_metadata, new_id,
)
from agent_forge.wiki import storage
from agent_forge.wiki.ratchet import (
    DEFAULT_SKILL, RatchetResult, load_skill, ratchet_session,
)
from agent_forge.wiki.ratchet.bundle import build_session_bundle


# ── Skill loader ──────────────────────────────────────────────────────────────

def test_load_skill_returns_default_when_missing(tmp_path):
    assert load_skill(tmp_path) == DEFAULT_SKILL


def test_load_skill_returns_user_override(tmp_path):
    sd = storage.skills_dir(tmp_path)
    sd.mkdir(parents=True)
    (sd / "ratchet.md").write_text("Custom prompt for ratchet.\n", encoding="utf-8")
    assert load_skill(tmp_path) == "Custom prompt for ratchet."


def test_load_skill_falls_back_when_file_blank(tmp_path):
    sd = storage.skills_dir(tmp_path)
    sd.mkdir(parents=True)
    (sd / "ratchet.md").write_text("   \n\n", encoding="utf-8")
    assert load_skill(tmp_path) == DEFAULT_SKILL


# ── Bundle builder ────────────────────────────────────────────────────────────

def _make_session(tmp_home, monkeypatch) -> str:
    """Set up a session JSONL with a small user/assistant/tool exchange."""
    monkeypatch.setenv("HOME", str(tmp_home))
    sid = new_id()
    append_metadata(sid, "claude-test", str(tmp_home))
    append_message(sid, UserMessage(content="how does refund work?"))
    append_message(sid, AssistantMessage(
        content=(
            TextContent(text="It uses an idempotency key; see refund.py."),
            ToolCallContent(id="t1", name="Read", arguments={"path": "src/refund.py"}),
        ),
        stop_reason="tool_use",
    ))
    append_message(sid, ToolResultMessage(
        tool_call_id="t1",
        content="def refund(x):\n    ...\n",
        is_error=False,
    ))
    append_message(sid, AssistantMessage(
        content=(TextContent(text="Confirmed — idempotency lives in refund.py:42."),),
        stop_reason="end_turn",
    ))
    return sid


def test_bundle_empty_session_returns_empty_string(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert build_session_bundle("does-not-exist") == ""


def test_bundle_renders_user_assistant_tool(tmp_path, monkeypatch):
    sid = _make_session(tmp_path, monkeypatch)
    bundle = build_session_bundle(sid)
    assert "## User" in bundle
    assert "how does refund work" in bundle
    assert "## Assistant" in bundle
    assert "idempotency key" in bundle
    assert "→ Read(path='src/refund.py')" in bundle
    assert "## Tool results" in bundle
    assert "def refund" in bundle


def test_bundle_respects_max_chars(tmp_path, monkeypatch):
    sid = _make_session(tmp_path, monkeypatch)
    bundle = build_session_bundle(sid, max_chars=50)
    assert len(bundle) <= 80  # small overhead allowance for separator


# ── Runner with a fake provider ───────────────────────────────────────────────

class _FakeProvider:
    """Minimal LLMProvider that yields a single text response."""

    def __init__(self, response: str, *, error: str | None = None):
        self._response = response
        self._error = error

    async def stream(self, model, system, messages, tools, *,
                     signal=None, max_tokens=None, thinking="off"):
        from agent_forge.provider import (
            DoneEvent, StreamErrorEvent, TextDeltaEvent,
        )
        from agent_forge.messages import (
            AssistantMessage, TextContent, TokenUsage,
        )

        if self._error is not None:
            yield StreamErrorEvent(error=self._error, retryable=False)
            return

        for ch in self._response:
            yield TextDeltaEvent(delta=ch)
        msg = AssistantMessage(
            content=(TextContent(text=self._response),),
            stop_reason="end_turn",
            usage=TokenUsage(input=10, output=20),
        )
        yield DoneEvent(message=msg)


@pytest.mark.asyncio
async def test_ratchet_writes_when_llm_returns_insights(tmp_path, monkeypatch):
    sid = _make_session(tmp_path, monkeypatch)
    storage.ensure_layout(tmp_path)

    provider = _FakeProvider(
        "# Session insights\n\n- refund.py uses idempotency keys (file refund.py:42).\n"
    )
    from agent_forge.models import DEFAULT_MODEL

    res = await ratchet_session(tmp_path, sid, provider, DEFAULT_MODEL)
    assert isinstance(res, RatchetResult)
    assert res.wrote is True
    assert res.output_path is not None
    assert res.output_path.exists()
    body = res.output_path.read_text(encoding="utf-8")
    assert "source: ratchet" in body
    assert "idempotency keys" in body
    assert sid in body


@pytest.mark.asyncio
async def test_ratchet_skips_when_llm_returns_sentinel(tmp_path, monkeypatch):
    sid = _make_session(tmp_path, monkeypatch)
    storage.ensure_layout(tmp_path)

    provider = _FakeProvider("NOTHING TO RATCHET")
    from agent_forge.models import DEFAULT_MODEL

    res = await ratchet_session(tmp_path, sid, provider, DEFAULT_MODEL)
    assert res.wrote is False
    assert res.output_path is None
    # No file should have been written.
    sess_dir = storage.raw_notes_dir(tmp_path) / "session"
    assert not sess_dir.exists() or not list(sess_dir.glob("*.md"))


@pytest.mark.asyncio
async def test_ratchet_handles_provider_error(tmp_path, monkeypatch):
    sid = _make_session(tmp_path, monkeypatch)
    storage.ensure_layout(tmp_path)

    provider = _FakeProvider("", error="upstream 500")
    from agent_forge.models import DEFAULT_MODEL

    res = await ratchet_session(tmp_path, sid, provider, DEFAULT_MODEL)
    assert res.wrote is False
    assert res.error == "upstream 500"


@pytest.mark.asyncio
async def test_ratchet_dry_run_does_not_call_llm(tmp_path, monkeypatch):
    sid = _make_session(tmp_path, monkeypatch)
    storage.ensure_layout(tmp_path)

    class _Boom:
        async def stream(self, *a, **kw):
            raise AssertionError("LLM should not be called in dry-run")
            yield  # pragma: no cover  (turn into async generator)

    from agent_forge.models import DEFAULT_MODEL

    res = await ratchet_session(tmp_path, sid, _Boom(), DEFAULT_MODEL, dry_run=True)
    assert res.wrote is False
    assert "dry-run" in res.insights_text


@pytest.mark.asyncio
async def test_ratchet_empty_session_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    storage.ensure_layout(tmp_path)
    provider = _FakeProvider("anything")
    from agent_forge.models import DEFAULT_MODEL

    res = await ratchet_session(tmp_path, "no-such-session", provider, DEFAULT_MODEL)
    assert res.wrote is False
    assert res.error == "empty session"
