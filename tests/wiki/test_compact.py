"""Tests for wiki/compact/ — page-by-page LLM lint of curated/."""
from __future__ import annotations

import pytest

from agent_forge.wiki import storage
from agent_forge.wiki.compact import (
    DEFAULT_SKILL, CompactResult, compact_wiki, load_skill,
)


# ── Skill loader ──────────────────────────────────────────────────────────────

def test_load_skill_default(tmp_path):
    assert load_skill(tmp_path) == DEFAULT_SKILL


def test_load_skill_user_override(tmp_path):
    sd = storage.skills_dir(tmp_path)
    sd.mkdir(parents=True)
    (sd / "compact.md").write_text("Custom compact.\n", encoding="utf-8")
    assert load_skill(tmp_path) == "Custom compact."


# ── Runner with a fake provider ──────────────────────────────────────────────

class _FakeProvider:
    """Map filename hint in the user message to a canned response."""

    def __init__(self, default: str = "NO CHANGE", per_path: dict[str, str] | None = None,
                 error: str | None = None):
        self.default = default
        self.per_path = per_path or {}
        self.error = error
        self.calls = 0

    async def stream(self, model, system, messages, tools, *,
                     signal=None, max_tokens=None, thinking="off"):
        from agent_forge.provider import (
            DoneEvent, StreamErrorEvent, TextDeltaEvent,
        )
        from agent_forge.messages import (
            AssistantMessage, TextContent, TokenUsage,
        )
        self.calls += 1
        user = messages[0].content if messages else ""
        text = self.default
        for k, v in self.per_path.items():
            if k in user:
                text = v
                break
        if self.error is not None:
            yield StreamErrorEvent(error=self.error, retryable=False)
            return
        for ch in text:
            yield TextDeltaEvent(delta=ch)
        yield DoneEvent(message=AssistantMessage(
            content=(TextContent(text=text),),
            stop_reason="end_turn",
            usage=TokenUsage(input=1, output=2),
        ))


@pytest.mark.asyncio
async def test_compact_no_curated_returns_empty(tmp_path):
    from agent_forge.models import DEFAULT_MODEL
    res = await compact_wiki(tmp_path, _FakeProvider(), DEFAULT_MODEL)
    assert isinstance(res, CompactResult)
    assert res.rewrote == ()
    assert res.unchanged == ()


@pytest.mark.asyncio
async def test_compact_skips_no_change_files(tmp_path):
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True)
    (cdir / "onboarding.md").write_text("# Onboarding\nstuff\n", encoding="utf-8")
    from agent_forge.models import DEFAULT_MODEL
    provider = _FakeProvider(default="NO CHANGE")
    res = await compact_wiki(tmp_path, provider, DEFAULT_MODEL)
    assert len(res.rewrote) == 0
    assert len(res.unchanged) == 1
    # File untouched.
    assert "stuff" in (cdir / "onboarding.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_compact_rewrites_when_llm_returns_new_content(tmp_path):
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True)
    (cdir / "onboarding.md").write_text("# Onboarding\noriginal\n", encoding="utf-8")
    from agent_forge.models import DEFAULT_MODEL
    # The runner doesn't pass the filename in the user message; switch on
    # something we know IS in the message — the original body's content.
    provider = _FakeProvider(per_path={"original": "# Onboarding\nrewritten\n"})
    res = await compact_wiki(tmp_path, provider, DEFAULT_MODEL)
    assert len(res.rewrote) == 1
    body = (cdir / "onboarding.md").read_text(encoding="utf-8")
    assert body.startswith("<!-- compacted:")
    assert "rewritten" in body
    assert "original" not in body


@pytest.mark.asyncio
async def test_compact_records_errors(tmp_path):
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True)
    (cdir / "x.md").write_text("# X\n", encoding="utf-8")
    from agent_forge.models import DEFAULT_MODEL
    provider = _FakeProvider(error="boom")
    res = await compact_wiki(tmp_path, provider, DEFAULT_MODEL)
    assert len(res.errors) == 1
    assert "boom" in res.errors[0][1]


@pytest.mark.asyncio
async def test_compact_dry_run_calls_no_llm(tmp_path):
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True)
    (cdir / "x.md").write_text("# X\n", encoding="utf-8")
    from agent_forge.models import DEFAULT_MODEL

    class _Boom(_FakeProvider):
        async def stream(self, *a, **kw):  # type: ignore[override]
            raise AssertionError("LLM should not be called in dry run")
            yield  # pragma: no cover

    res = await compact_wiki(tmp_path, _Boom(), DEFAULT_MODEL, dry_run=True)
    assert res.rewrote == ()
    assert len(res.unchanged) == 1


@pytest.mark.asyncio
async def test_compact_logs_to_metrics(tmp_path):
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True)
    (cdir / "x.md").write_text("# X\n", encoding="utf-8")
    from agent_forge.models import DEFAULT_MODEL
    provider = _FakeProvider(default="NO CHANGE")
    await compact_wiki(tmp_path, provider, DEFAULT_MODEL)
    log = tmp_path / ".agent-forge" / "metrics" / "compact_log.jsonl"
    assert log.exists()
    assert "x.md" in log.read_text(encoding="utf-8")
