"""Tests for the public prompt-section composables."""
from __future__ import annotations

import pytest

from agent_forge.prompts import (
    CHAT_GUIDELINES, CHAT_IDENTITY,
    EXECUTE_GUIDELINES, EXECUTE_IDENTITY,
    PLAN_GUIDELINES, PLAN_IDENTITY,
    TOOLS_SECTION,
    VERIFY_GUIDELINES, VERIFY_IDENTITY,
    build_autonomous_prompt, build_chat_prompt, build_repo_map,
    discover_skills, environment_section, load_agents_doc, tools_section,
)
from agent_forge.system_prompt import SectionName, SystemPrompt
from agent_forge.tools import default_registry


# ── Static text constants exist and are non-empty ─────────────────────────────

@pytest.mark.parametrize("text", [
    TOOLS_SECTION, CHAT_IDENTITY, CHAT_GUIDELINES,
    PLAN_IDENTITY, PLAN_GUIDELINES,
    EXECUTE_IDENTITY, EXECUTE_GUIDELINES,
    VERIFY_IDENTITY, VERIFY_GUIDELINES,
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


# ── build_autonomous_prompt assembles a SystemPrompt ──────────────────────────

@pytest.mark.parametrize("phase,identity_substr", [
    ("plan",    "planning mode"),
    ("execute", "autonomous mode"),
    ("verify",  "verification mode"),
])
def test_build_autonomous_prompt_yields_phase_specific_identity(
    phase: str, identity_substr: str, tmp_path,
):
    sp = build_autonomous_prompt(
        phase, cwd=str(tmp_path), tool_registry=default_registry(),
        branch="b", worktree_path=str(tmp_path),
    )
    assert isinstance(sp, SystemPrompt)
    sections = sp.build()
    blob = "\n".join(s.text for s in sections)
    assert identity_substr in blob
    assert TOOLS_SECTION in blob


def test_build_autonomous_prompt_rejects_unknown_phase(tmp_path):
    with pytest.raises(ValueError, match="Unknown phase"):
        build_autonomous_prompt(
            "ship",  # type: ignore[arg-type]
            cwd=str(tmp_path), tool_registry=default_registry(),
        )


def test_build_autonomous_prompt_uses_skills_cwd(tmp_path):
    """skills_cwd lets autonomous discover skills from the repo even when cwd is a worktree."""
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir(); worktree.mkdir()
    skills = repo / ".agent-forge" / "skills"
    skills.mkdir(parents=True)
    (skills / "demo.md").write_text("Demo skill description.\n")

    sp = build_autonomous_prompt(
        "execute",
        cwd=str(worktree), tool_registry=default_registry(),
        branch="b", worktree_path=str(worktree),
        skills_cwd=str(repo),
    )
    blob = "\n".join(s.text for s in sp.build())
    assert "/demo" in blob
    assert "Demo skill description." in blob


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
