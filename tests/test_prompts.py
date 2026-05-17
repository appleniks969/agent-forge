"""Tests for the public prompt-section composables."""
from __future__ import annotations

import pytest

from agent_forge.prompts import (
    CHAT_GUIDELINES, CHAT_IDENTITY,
    TOOLS_SECTION,
    build_chat_prompt, build_repo_map,
    discover_skills, environment_section, load_agents_doc, tools_section,
)
from agent_forge.tools import default_registry


# ── Static text constants exist and are non-empty ─────────────────────────────

@pytest.mark.parametrize("text", [
    TOOLS_SECTION, CHAT_IDENTITY, CHAT_GUIDELINES,
])
def test_static_prompt_constants_are_non_empty(text: str):
    assert isinstance(text, str)
    assert len(text.strip()) > 50  # all are substantial


# ── tools_section() augments with plugin tools ────────────────────────────────

def test_tools_section_returns_base_for_default_registry():
    assert tools_section(default_registry()) == TOOLS_SECTION


class _FakePluginTool:
    name = "Sleep"
    description = "Sleep for N seconds."


def test_tools_section_appends_plugin_tools():
    reg = default_registry()
    reg.register(_FakePluginTool())
    out = tools_section(reg)
    assert out.startswith(TOOLS_SECTION)
    assert "Plugin tools:" in out
    assert "- Sleep: Sleep for N seconds." in out


# ── environment_section ───────────────────────────────────────────────────────

def test_environment_section_minimal(tmp_path):
    out = environment_section(str(tmp_path))
    assert f"Working directory: {tmp_path}" in out
    assert "Date: " in out
    assert "Branch:" not in out


def test_environment_section_with_branch_and_worktree(tmp_path):
    out = environment_section(
        str(tmp_path), branch="agent/123",
        worktree_path="/tmp/worktree-x", include_path_note=False,
    )
    assert "Working directory: /tmp/worktree-x" in out
    assert "Branch: agent/123" in out
    assert "All file paths" not in out


# ── load_agents_doc / build_repo_map / discover_skills ────────────────────────

def test_load_agents_doc_finds_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agent rules\n- be nice\n")
    text = load_agents_doc(str(tmp_path))
    assert text and "be nice" in text


def test_load_agents_doc_falls_back_to_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# CLAUDE rules\n")
    assert load_agents_doc(str(tmp_path)).startswith("# CLAUDE")


def test_load_agents_doc_returns_none_when_absent(tmp_path):
    assert load_agents_doc(str(tmp_path)) is None


def test_build_repo_map_lists_files(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y")
    out = build_repo_map(str(tmp_path))
    assert out and "Repository files:" in out
    assert "a.py" in out
    assert "sub/b.py" in out


def test_discover_skills_returns_summary(tmp_path):
    skills = tmp_path / ".agent-forge" / "skills"
    skills.mkdir(parents=True)
    (skills / "implement.md").write_text("# Implement\nImplement a feature end-to-end.\n")
    out = discover_skills(str(tmp_path))
    assert out and "/implement" in out
    assert "Implement a feature end-to-end." in out


def test_discover_skills_returns_none_when_dir_missing(tmp_path):
    assert discover_skills(str(tmp_path)) is None


# ── build_chat_prompt smoke test ──────────────────────────────────────────────

class _FakeCfg:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.custom_system_prompt = None


def test_build_chat_prompt_includes_chat_identity(tmp_path):
    sp = build_chat_prompt(_FakeCfg(str(tmp_path)), default_registry())
    blob = "\n".join(s.text for s in sp.build())
    assert "interactive chat mode" in blob
    assert TOOLS_SECTION in blob


# ── build_chat_prompt_async: runs file I/O off the event loop ────────────────

import asyncio
import time

from agent_forge.prompts import build_chat_prompt_async
from agent_forge.system_prompt import SystemPrompt


@pytest.mark.asyncio
async def test_build_chat_prompt_async_returns_system_prompt(tmp_path):
    """The async composer returns a usable SystemPrompt."""
    sp = await build_chat_prompt_async(_FakeCfg(str(tmp_path)), default_registry())
    assert isinstance(sp, SystemPrompt)
    sections = sp.build()
    blob = "\n".join(s.text for s in sections)
    assert "interactive chat mode" in blob


@pytest.mark.asyncio
async def test_build_chat_prompt_async_does_not_block_event_loop(tmp_path, monkeypatch):
    """While build_repo_map is running in a thread, other tasks make progress."""
    original = build_repo_map

    def slow(cwd):
        time.sleep(0.2)
        return original(cwd)

    monkeypatch.setattr("agent_forge.prompts.build_repo_map", slow)

    async def ticker():
        ticks = 0
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1
        return ticks

    cfg = _FakeCfg(str(tmp_path))
    ticks_task = asyncio.create_task(ticker())
    sp = await build_chat_prompt_async(cfg, default_registry())
    ticks = await ticks_task

    assert isinstance(sp, SystemPrompt)
    # If build_repo_map had blocked the event loop, ticker would have made
    # zero progress during the 200 ms sleep. >5 ticks proves it ran in a thread.
    assert ticks > 5, f"event loop blocked: only {ticks} ticks"
